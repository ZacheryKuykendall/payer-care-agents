"""In-memory hybrid index: BM25 + vector, fused with reciprocal rank fusion.

This mirrors the retrieval shape of Azure AI Search hybrid queries closely enough
that agent code, prompt construction and citation handling are identical in both
modes. What it does *not* mirror is semantic quality, reranking, or scale.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

from payer_agents.decisions.mock import tokenize
from payer_agents.retrieval.embeddings import Embedder, HashingEmbedder, cosine
from payer_agents.retrieval.types import Chunk, SearchHit, SearchIndex

BM25_K1 = 1.4
BM25_B = 0.75
#: RRF constant. 60 is the conventional default and the value Azure AI Search uses.
RRF_K = 60


class InMemoryHybridIndex(SearchIndex):
    """Small, dependency-free hybrid index for mock mode and tests."""

    name = "in-memory-hybrid"

    def __init__(self, embedder: Embedder | None = None, rrf_k: int = RRF_K) -> None:
        self.embedder = embedder or HashingEmbedder()
        self.rrf_k = rrf_k
        self._chunks: dict[str, Chunk] = {}
        self._tokens: dict[str, Counter[str]] = {}
        self._lengths: dict[str, int] = {}
        self._vectors: dict[str, list[float]] = {}
        self._document_frequency: Counter[str] = Counter()

    # -- indexing --------------------------------------------------------------

    def add(self, chunks: list[Chunk]) -> None:
        for chunk in chunks:
            if chunk.chunk_id in self._chunks:
                self._remove(chunk.chunk_id)
            searchable = f"{chunk.title}\n{chunk.section}\n{chunk.text}"
            counts = Counter(tokenize(searchable))
            self._chunks[chunk.chunk_id] = chunk
            self._tokens[chunk.chunk_id] = counts
            self._lengths[chunk.chunk_id] = sum(counts.values())
            self._vectors[chunk.chunk_id] = self.embedder.embed(searchable)
            for term in counts:
                self._document_frequency[term] += 1

    def _remove(self, chunk_id: str) -> None:
        for term in self._tokens.pop(chunk_id, Counter()):
            self._document_frequency[term] -= 1
            if self._document_frequency[term] <= 0:
                del self._document_frequency[term]
        self._chunks.pop(chunk_id, None)
        self._lengths.pop(chunk_id, None)
        self._vectors.pop(chunk_id, None)

    def __len__(self) -> int:
        return len(self._chunks)

    @property
    def average_length(self) -> float:
        if not self._lengths:
            return 0.0
        return sum(self._lengths.values()) / len(self._lengths)

    # -- scoring ---------------------------------------------------------------

    def _bm25(self, query_terms: Counter[str], candidates: list[str]) -> dict[str, float]:
        total_docs = len(self._chunks)
        if total_docs == 0:
            return {}
        avg_len = self.average_length or 1.0
        scores: dict[str, float] = defaultdict(float)
        for term in query_terms:
            doc_freq = self._document_frequency.get(term, 0)
            if doc_freq == 0:
                continue
            idf = math.log(1 + (total_docs - doc_freq + 0.5) / (doc_freq + 0.5))
            for chunk_id in candidates:
                freq = self._tokens[chunk_id].get(term, 0)
                if freq == 0:
                    continue
                length_norm = 1 - BM25_B + BM25_B * (self._lengths[chunk_id] / avg_len)
                scores[chunk_id] += idf * (freq * (BM25_K1 + 1)) / (freq + BM25_K1 * length_norm)
        return dict(scores)

    def _vector_scores(self, query: str, candidates: list[str]) -> dict[str, float]:
        query_vector = self.embedder.embed(query)
        if not any(query_vector):
            return {}
        return {cid: cosine(query_vector, self._vectors[cid]) for cid in candidates}

    @staticmethod
    def _ranks(scores: dict[str, float]) -> dict[str, int]:
        ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return {chunk_id: rank for rank, (chunk_id, _) in enumerate(ordered, start=1)}

    # -- query -----------------------------------------------------------------

    def search(
        self, query: str, top_k: int = 4, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        candidates = [cid for cid, chunk in self._chunks.items() if chunk.matches(filters)]
        if not candidates or top_k <= 0:
            return []

        query_terms = Counter(tokenize(query))
        lexical = self._bm25(query_terms, candidates)
        vector = {
            cid: score
            for cid, score in self._vector_scores(query, candidates).items()
            if score > 0
        }

        lexical_ranks = self._ranks(lexical)
        vector_ranks = self._ranks(vector)

        fused: dict[str, float] = {}
        for chunk_id in set(lexical_ranks) | set(vector_ranks):
            score = 0.0
            if chunk_id in lexical_ranks:
                score += 1.0 / (self.rrf_k + lexical_ranks[chunk_id])
            if chunk_id in vector_ranks:
                score += 1.0 / (self.rrf_k + vector_ranks[chunk_id])
            fused[chunk_id] = score

        ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
        return [
            SearchHit(
                chunk=self._chunks[chunk_id],
                score=score,
                lexical_score=lexical.get(chunk_id, 0.0),
                vector_score=vector.get(chunk_id, 0.0),
            )
            for chunk_id, score in ranked
        ]
