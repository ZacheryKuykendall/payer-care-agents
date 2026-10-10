"""Answer composition.

Mock mode composes answers *extractively*: it selects the most relevant lines from
the retrieved passages and assembles them with citation markers. It never writes a
sentence that is not in a source document.

That is a strange way to build a chatbot and a very good way to build a demo of a
grounded one. Every claim is traceable by construction, so the grounding judge, the
citation UI and the audit trail all exercise real behaviour rather than a stub. The
cost is register: the output reads like a well-organised policy extract, not like a
service representative. :class:`FoundryChatComposer` is the production path, and the
README is explicit that fluency is what mock mode gives up.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from payer_agents.decisions.mock import tokenize
from payer_agents.domain import Citation
from payer_agents.retrieval.types import SearchHit

BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)\s*")
ITEM_START = re.compile(r"^\s*(?:[-*]\s|\d+\.\s|\|)")
SENTENCE_END = re.compile(r"(?<=[.:;])\s+(?=[A-Z(])")
MAX_EVIDENCE_LINES = 4
MIN_LINE_LENGTH = 24


class ComposedAnswer(BaseModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class Composer(ABC):
    """Turns retrieved evidence plus structured facts into member-facing text."""

    name: str = "composer"

    @abstractmethod
    def compose(
        self,
        *,
        lead_in: str,
        query: str,
        hits: list[SearchHit],
        facts: list[str] | None = None,
        closing: str | None = None,
    ) -> ComposedAnswer: ...


def split_lines(text: str) -> list[str]:
    """Split a passage into candidate evidence lines.

    Source markdown is hard-wrapped, so a naive ``splitlines`` yields sentence
    fragments -- and a fragment quoted back to a member reads as a bug. Physical
    lines are re-joined into logical items first: a table row, a list item, or a
    paragraph sentence. Tables are first-class because benefit documents are mostly
    tables.
    """
    items: list[str] = []
    buffer: list[str] = []
    buffer_is_item = False

    def flush() -> None:
        nonlocal buffer, buffer_is_item
        if not buffer:
            return
        joined = " ".join(buffer).strip()
        if buffer_is_item:
            items.append(joined)
        else:
            items.extend(part.strip() for part in SENTENCE_END.split(joined) if part.strip())
        buffer = []
        buffer_is_item = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(">") or set(line) <= set("-| "):
            flush()
            continue
        if ITEM_START.match(line):
            flush()
            buffer_is_item = True
        buffer.append(line)
    flush()
    return items


def tidy(line: str) -> str:
    """Normalise a markdown table row or bullet into a readable clause."""
    line = BULLET.sub("", line.strip())
    if "|" in line:
        cells = [c.strip() for c in line.strip("|").split("|") if c.strip()]
        if len(cells) >= 2:
            return f"{cells[0]}: {' / '.join(cells[1:])}"
        line = " ".join(cells)
    return line.strip()


def score_line(line: str, query_tokens: set[str]) -> float:
    tokens = tokenize(line)
    if not tokens or not query_tokens:
        return 0.0
    overlap = len(tokens & query_tokens)
    # Normalising by sqrt keeps a long, dense table row from beating a precise
    # one-sentence rule purely on length.
    return overlap / (len(tokens) ** 0.5)


class ExtractiveComposer(Composer):
    """Selects and formats source lines. Writes no new claims."""

    name = "extractive"

    def __init__(self, max_lines: int = MAX_EVIDENCE_LINES, max_sources: int = 3) -> None:
        self.max_lines = max_lines
        self.max_sources = max_sources

    def compose(
        self,
        *,
        lead_in: str,
        query: str,
        hits: list[SearchHit],
        facts: list[str] | None = None,
        closing: str | None = None,
    ) -> ComposedAnswer:
        query_tokens = tokenize(query)
        candidates = hits[: self.max_sources]

        scored: list[tuple[float, int, str]] = []
        for position, hit in enumerate(candidates):
            for line in split_lines(hit.chunk.text):
                cleaned = tidy(line)
                if len(cleaned) < MIN_LINE_LENGTH:
                    continue
                score = score_line(cleaned, query_tokens)
                if score > 0:
                    scored.append((score, position, cleaned))

        scored.sort(key=lambda item: (-item[0], item[1], item[2]))

        # Cite only what is actually quoted, numbered in order of first use. A source
        # list containing documents the answer never drew on reads as padding and
        # makes the citations harder to check, not easier.
        chosen: list[tuple[int, str]] = []
        seen: set[str] = set()
        for _, position, line in scored:
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            chosen.append((position, line))
            if len(chosen) >= self.max_lines:
                break

        order: dict[int, int] = {}
        for position, _ in chosen:
            if position not in order:
                order[position] = len(order) + 1
        citations = [candidates[position].to_citation() for position in sorted(order,
                                                                               key=order.get)]  # type: ignore[arg-type]
        evidence = [f"{line} [{order[position]}]" for position, line in chosen]

        parts: list[str] = [lead_in.strip()]
        for fact in facts or []:
            parts.append(f"- {fact}")
        if evidence:
            if facts:
                parts.append("")
                parts.append("From the plan documents:")
            parts.extend(f"- {line}" for line in evidence)
        elif not facts:
            parts.append(
                "- I could not find a passage in the plan documents that answers this "
                "directly, so I am handing it to a member services representative."
            )

        if citations:
            parts.append("")
            parts.append("Sources:")
            for index, citation in enumerate(citations, start=1):
                section = f" - {citation.section}" if citation.section else ""
                parts.append(f"[{index}] {citation.title}{section}")

        if closing:
            parts.append("")
            parts.append(closing.strip())

        return ComposedAnswer(
            text="\n".join(parts).strip(), citations=citations, evidence=evidence
        )


class FoundryChatComposer(Composer):
    """Grounded generation against a Foundry chat deployment.

    The system prompt is constrained the same way the extractive composer is
    constrained by construction: answer only from the supplied passages, cite by
    index, and say so when the passages do not cover the question. The grounding
    judge then checks whether it complied, because a prompt instruction is a request,
    not a control.
    """

    name = "foundry-chat"

    SYSTEM_PROMPT = (
        "You are a health plan member services assistant. Answer only from the "
        "numbered passages provided. Cite every factual claim with its passage number "
        "in square brackets. If the passages do not answer the question, say so "
        "plainly and do not guess. Never give medical advice, never state or imply a "
        "coverage determination that is not already in the passages or the supplied "
        "facts, and never speculate about an outcome. Keep the answer under 150 words."
    )

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        *,
        api_key: str | None = None,
        client: Any | None = None,
        api_version: str = "2024-10-21",
        timeout: float = 30.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.deployment = deployment
        self.api_key = api_key
        self.api_version = api_version
        if client is None:
            import httpx

            client = httpx.Client(timeout=timeout)
        self._client = client

    def compose(
        self,
        *,
        lead_in: str,
        query: str,
        hits: list[SearchHit],
        facts: list[str] | None = None,
        closing: str | None = None,
    ) -> ComposedAnswer:
        citations = [hit.to_citation() for hit in hits[:3]]
        passages = "\n\n".join(
            f"[{index}] {citation.title} - {citation.section}\n{citation.snippet}"
            for index, citation in enumerate(citations, start=1)
        )
        fact_block = "\n".join(f"- {fact}" for fact in facts or [])
        user = (
            f"Member question: {query}\n\n"
            f"Verified facts from plan systems:\n{fact_block or '- none'}\n\n"
            f"Passages:\n{passages or 'none'}"
        )
        url = (
            f"{self.endpoint}/openai/deployments/{self.deployment}/chat/completions"
            f"?api-version={self.api_version}"
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        response = self._client.post(
            url,
            headers=headers,
            json={
                "messages": [
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.1,
                "max_tokens": 400,
            },
        )
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"].strip()
        if closing:
            text = f"{text}\n\n{closing.strip()}"
        return ComposedAnswer(text=text, citations=citations, evidence=[])
