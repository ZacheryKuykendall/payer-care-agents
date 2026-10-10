"""Azure AI Search adapter.

Implements the same :class:`~payer_agents.retrieval.types.SearchIndex` contract using
a hybrid query (BM25 + vector) with semantic ranking, which is the retrieval
configuration the README recommends for payer policy corpora.

``azure-search-documents`` is imported lazily. Importing this module in mock mode is
free; constructing the class is what requires the extra.
"""

from __future__ import annotations

from typing import Any

from payer_agents.retrieval.embeddings import Embedder
from payer_agents.retrieval.types import Chunk, SearchHit, SearchIndex

VECTOR_FIELD = "contentVector"
SEMANTIC_CONFIG = "payer-semantic"


class AzureAISearchIndex(SearchIndex):
    """Hybrid retrieval against one Azure AI Search index."""

    name = "azure-ai-search"

    def __init__(
        self,
        endpoint: str,
        index_name: str,
        embedder: Embedder,
        *,
        credential: Any | None = None,
        api_key: str | None = None,
        client: Any | None = None,
        semantic_configuration: str | None = SEMANTIC_CONFIG,
    ) -> None:
        self.endpoint = endpoint
        self.index_name = index_name
        self.embedder = embedder
        self.semantic_configuration = semantic_configuration
        self._client = client or self._build_client(endpoint, index_name, credential, api_key)

    @staticmethod
    def _build_client(
        endpoint: str, index_name: str, credential: Any | None, api_key: str | None
    ) -> Any:
        try:
            from azure.search.documents import SearchClient
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "AzureAISearchIndex needs the 'azure' extra: pip install '.[azure]'"
            ) from exc
        if credential is None:
            if api_key:
                from azure.core.credentials import AzureKeyCredential

                credential = AzureKeyCredential(api_key)
            else:
                from azure.identity import DefaultAzureCredential

                credential = DefaultAzureCredential()
        return SearchClient(endpoint=endpoint, index_name=index_name, credential=credential)

    # -- indexing --------------------------------------------------------------

    def add(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        vectors = self.embedder.embed_batch(
            [f"{c.title}\n{c.section}\n{c.text}" for c in chunks]
        )
        documents = [
            {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "title": chunk.title,
                "section": chunk.section,
                "content": chunk.text,
                "doc_type": chunk.doc_type,
                "plan_ids": chunk.plan_ids,
                "url": chunk.url,
                VECTOR_FIELD: vector,
            }
            for chunk, vector in zip(chunks, vectors)
        ]
        self._client.merge_or_upload_documents(documents=documents)

    # -- query -----------------------------------------------------------------

    @staticmethod
    def build_filter(filters: dict[str, Any] | None) -> str | None:
        """Translate the portable filter dict into an OData filter expression.

        ``plan_id`` is special-cased to the collection-contains form so that a chunk
        with no plan list stays visible to every plan.
        """
        if not filters:
            return None
        clauses: list[str] = []
        for key, value in filters.items():
            if value is None:
                continue
            if key == "plan_id":
                clauses.append(
                    f"(plan_ids/any(p: p eq '{_escape(value)}') or not plan_ids/any())"
                )
            elif isinstance(value, (list, tuple, set)):
                joined = " or ".join(f"{key} eq '{_escape(v)}'" for v in value)
                clauses.append(f"({joined})")
            else:
                clauses.append(f"{key} eq '{_escape(value)}'")
        return " and ".join(clauses) if clauses else None

    def search(
        self, query: str, top_k: int = 4, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        try:
            from azure.search.documents.models import VectorizedQuery
        except ImportError:  # pragma: no cover - depends on optional extra
            VectorizedQuery = None  # type: ignore[assignment]

        kwargs: dict[str, Any] = {
            "search_text": query,
            "top": top_k,
            "filter": self.build_filter(filters),
        }
        if VectorizedQuery is not None:
            kwargs["vector_queries"] = [
                VectorizedQuery(
                    vector=self.embedder.embed(query),
                    k_nearest_neighbors=max(top_k * 4, 20),
                    fields=VECTOR_FIELD,
                )
            ]
        if self.semantic_configuration:
            kwargs["query_type"] = "semantic"
            kwargs["semantic_configuration_name"] = self.semantic_configuration

        hits: list[SearchHit] = []
        for result in self._client.search(**kwargs):
            chunk = Chunk(
                chunk_id=result["chunk_id"],
                doc_id=result.get("doc_id", result["chunk_id"]),
                title=result.get("title", ""),
                section=result.get("section", ""),
                text=result.get("content", ""),
                doc_type=result.get("doc_type", "policy"),
                plan_ids=list(result.get("plan_ids") or []),
                url=result.get("url"),
            )
            score = float(
                result.get("@search.reranker_score") or result.get("@search.score") or 0.0
            )
            hits.append(SearchHit(chunk=chunk, score=score, lexical_score=score))
        return hits


def _escape(value: Any) -> str:
    return str(value).replace("'", "''")
