"""Markdown-aware chunking.

Payer source documents are heavily sectioned -- Evidence of Coverage tables, medical
policy criteria lists -- and the section heading is usually the single most useful
thing in a citation. So the splitter is heading-first and only falls back to length
splitting inside an oversized section.
"""

from __future__ import annotations

import re

from payer_agents.retrieval.types import Chunk, Document

HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)

DEFAULT_MAX_CHARS = 900
DEFAULT_OVERLAP = 120


def split_sections(body: str) -> list[tuple[str, str]]:
    """Split markdown into ``(heading, text)`` pairs, preserving pre-heading text."""
    matches = list(HEADING.finditer(body))
    if not matches:
        stripped = body.strip()
        return [("Overview", stripped)] if stripped else []

    sections: list[tuple[str, str]] = []
    preamble = body[: matches[0].start()].strip()
    if preamble:
        sections.append(("Overview", preamble))

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        text = body[match.end() : end].strip()
        if text:
            sections.append((match.group(2).strip(), text))
    return sections


def split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    """Length-split on paragraph boundaries, with a small overlap for continuity."""
    if len(text) <= max_chars:
        return [text]

    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    pieces: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= max_chars or not current:
            current = candidate
            continue
        pieces.append(current)
        tail = current[-overlap:] if overlap else ""
        current = f"{tail}\n\n{paragraph}".strip() if tail else paragraph
    if current:
        pieces.append(current)
    return pieces


def chunk_document(
    document: Document,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Turn one document into indexable chunks."""
    chunks: list[Chunk] = []
    for section_index, (heading, text) in enumerate(split_sections(document.body)):
        for piece_index, piece in enumerate(split_long(text, max_chars, overlap)):
            chunks.append(
                Chunk(
                    chunk_id=f"{document.doc_id}#{section_index:02d}-{piece_index:02d}",
                    doc_id=document.doc_id,
                    title=document.title,
                    section=heading,
                    text=piece,
                    doc_type=document.doc_type,
                    plan_ids=list(document.plan_ids),
                    url=document.url,
                    metadata={
                        **document.metadata,
                        "effective_date": document.effective_date,
                    },
                )
            )
    return chunks


def chunk_documents(
    documents: list[Document],
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    return [c for doc in documents for c in chunk_document(doc, max_chars, overlap)]
