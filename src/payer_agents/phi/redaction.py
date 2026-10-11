"""Pattern-based de-identification with reversible tokens.

Two honest caveats, stated here because they belong next to the code rather than in a
slide:

1. Regex de-identification catches structured identifiers. It does not catch free-text
   names, employer names, or a rare diagnosis that identifies someone in a small
   population. For a production PHI boundary this is one control in a stack that also
   includes Azure AI Language PII detection, a reviewed allow-list of fields leaving
   the trust boundary, and a BAA with every processor.
2. Redaction is for what gets *logged and shown*, and for narrowing what reaches a
   model when the deployment does not sit inside the compliance boundary. It is not a
   substitute for the boundary.

Tokens are reversible inside the process so a tool call can still use the real member
identifier. The token map never leaves :class:`Redactor`.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field


class PhiCategory(str, Enum):
    MEMBER_ID = "member_id"
    SSN = "ssn"
    MRN = "mrn"
    PHONE = "phone"
    EMAIL = "email"
    DATE_OF_BIRTH = "date_of_birth"
    POSTAL_CODE = "postal_code"


#: Order matters: the most specific pattern must win, so SSN is tried before phone and
#: member identifiers before generic digit runs.
PATTERNS: list[tuple[PhiCategory, re.Pattern[str]]] = [
    (PhiCategory.SSN, re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    (PhiCategory.MEMBER_ID, re.compile(r"\b(?:NW|MBR|MEM)[-\s]?\d{5,9}\b", re.IGNORECASE)),
    (PhiCategory.MRN, re.compile(r"\bMRN[:#\s]*\d{5,10}\b", re.IGNORECASE)),
    (
        PhiCategory.EMAIL,
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    (
        PhiCategory.PHONE,
        re.compile(r"(?<!\w)(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\w)"),
    ),
    (
        PhiCategory.DATE_OF_BIRTH,
        re.compile(r"\b(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:19|20)\d{2}\b"),
    ),
    (PhiCategory.POSTAL_CODE, re.compile(r"\b\d{5}-\d{4}\b")),
]

#: Reference numbers look like identifiers but are plan artefacts, not PHI. Stripping
#: them would break the prior-authorization flow for no privacy gain.
ALLOWED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bPA-\d{6,10}\b", re.IGNORECASE),
    re.compile(r"\bCLM-\d{6,10}\b", re.IGNORECASE),
]


class RedactionSpan(BaseModel):
    category: PhiCategory
    token: str
    start: int
    end: int


class RedactionResult(BaseModel):
    """Redacted text plus enough metadata to audit the redaction itself."""

    text: str
    original_length: int
    spans: list[RedactionSpan] = Field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for span in self.spans:
            tally[span.category.value] = tally.get(span.category.value, 0) + 1
        return tally

    @property
    def redacted(self) -> bool:
        return bool(self.spans)


class Redactor:
    """Replace structured identifiers with stable, reversible tokens."""

    def __init__(self, patterns: list[tuple[PhiCategory, re.Pattern[str]]] | None = None) -> None:
        self.patterns = patterns if patterns is not None else PATTERNS
        self._to_token: dict[str, str] = {}
        self._to_value: dict[str, str] = {}
        self._counters: dict[PhiCategory, int] = {}

    def _token_for(self, category: PhiCategory, value: str) -> str:
        key = f"{category.value}:{value.upper()}"
        existing = self._to_token.get(key)
        if existing is not None:
            return existing
        index = self._counters.get(category, 0) + 1
        self._counters[category] = index
        token = f"[{category.value.upper()}_{index}]"
        self._to_token[key] = token
        self._to_value[token] = value
        return token

    @staticmethod
    def _allowed_spans(text: str) -> list[tuple[int, int]]:
        return [m.span() for pattern in ALLOWED_PATTERNS for m in pattern.finditer(text)]

    def redact(self, text: str) -> RedactionResult:
        """Return ``text`` with identifiers replaced by tokens."""
        if not text:
            return RedactionResult(text="", original_length=0)

        protected = self._allowed_spans(text)
        matches: list[tuple[int, int, PhiCategory, str]] = []
        claimed: list[tuple[int, int]] = []

        for category, pattern in self.patterns:
            for match in pattern.finditer(text):
                start, end = match.span()
                if _overlaps(start, end, protected) or _overlaps(start, end, claimed):
                    continue
                claimed.append((start, end))
                matches.append((start, end, category, match.group(0)))

        matches.sort(key=lambda m: m[0])
        pieces: list[str] = []
        spans: list[RedactionSpan] = []
        cursor = 0
        for start, end, category, value in matches:
            token = self._token_for(category, value)
            pieces.append(text[cursor:start])
            spans.append(
                RedactionSpan(
                    category=category,
                    token=token,
                    start=sum(len(p) for p in pieces),
                    end=sum(len(p) for p in pieces) + len(token),
                )
            )
            pieces.append(token)
            cursor = end
        pieces.append(text[cursor:])

        return RedactionResult(text="".join(pieces), original_length=len(text), spans=spans)

    def rehydrate(self, text: str) -> str:
        """Put the original values back. Used when calling a tool inside the boundary."""
        for token, value in self._to_value.items():
            text = text.replace(token, value)
        return text

    def lookup(self, token: str) -> str | None:
        return self._to_value.get(token)

    def first_value(self, category: PhiCategory) -> str | None:
        """First redacted value of a category, e.g. the member ID a tool needs."""
        prefix = f"{category.value}:"
        for key, token in self._to_token.items():
            if key.startswith(prefix):
                return self._to_value[token]
        return None


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < s_end and end > s_start for s_start, s_end in spans)
