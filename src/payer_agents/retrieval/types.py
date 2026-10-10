"""Retrieval contracts.

The specialist agents only ever see :class:`SearchHit`. Swapping the in-memory index
for Azure AI Search is therefore a composition-root change, not an agent change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from payer_agents.domain import Citation


class Document(BaseModel):
    """A source document before chunking."""

    doc_id: str
    title: str
    body: str
    doc_type: str = "policy"
    plan_ids: list[str] = Field(default_factory=list)
    effective_date: str | None = None
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Chunk(BaseModel):
    """An indexed passage. ``section`` is what makes a citation navigable."""

    chunk_id: str
    doc_id: str
    title: str
    section: str
    text: str
    doc_type: str = "policy"
    plan_ids: list[str] = Field(default_factory=list)
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def matches(self, filters: dict[str, Any] | None) -> bool:
        """Metadata filter used to keep one plan's benefits out of another's answer."""
        if not filters:
            return True
        for key, expected in filters.items():
            if expected is None:
                continue
            if key == "plan_id":
                # An empty plan list means "applies to every plan".
                if self.plan_ids and expected not in self.plan_ids:
                    return False
                continue
            actual = getattr(self, key, None)
            if actual is None:
                actual = self.metadata.get(key)
            if isinstance(expected, (list, tuple, set)):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True


class SearchHit(BaseModel):
    """A retrieved chunk with its fused relevance score."""

    chunk: Chunk
    score: float
    lexical_score: float = 0.0
    vector_score: float = 0.0

    def to_citation(self) -> Citation:
        return Citation(
            source_id=self.chunk.chunk_id,
            title=self.chunk.title,
            snippet=self.chunk.text.strip(),
            score=self.score,
            section=self.chunk.section,
            url=self.chunk.url,
        )


class SearchIndex(ABC):
    """Minimal retrieval surface: add documents, search them."""

    name: str = "search-index"

    @abstractmethod
    def add(self, chunks: list[Chunk]) -> None:
        """Index a batch of chunks."""

    @abstractmethod
    def search(
        self, query: str, top_k: int = 4, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        """Return the best matching chunks, highest score first."""

    def __len__(self) -> int:  # pragma: no cover - overridden where meaningful
        return 0
