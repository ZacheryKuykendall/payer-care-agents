"""Load the synthetic document corpus from disk.

Documents are markdown with a small YAML front matter block. Keeping the corpus as
reviewable markdown rather than a fixture blob matters here: a hiring manager or a
compliance reviewer can open ``data/corpus`` and confirm for themselves that nothing
in it resembles real member data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from payer_agents.config import DATA_ROOT
from payer_agents.retrieval.chunking import chunk_documents
from payer_agents.retrieval.embeddings import Embedder
from payer_agents.retrieval.memory_index import InMemoryHybridIndex
from payer_agents.retrieval.types import Document, SearchIndex

FRONT_MATTER_DELIMITER = "---"

#: Which index each ``doc_type`` lands in. Two indexes, not one, because benefits and
#: medical policy have different owners, different refresh cadences and different
#: review boards -- splitting them in the architecture mirrors how they are governed.
INDEX_FOR_DOC_TYPE = {
    "benefits": "benefits",
    "formulary": "benefits",
    "policy": "policy",
    "utilization_management": "policy",
}


class CorpusError(RuntimeError):
    """A source document is malformed."""


def parse_document(text: str, fallback_id: str) -> Document:
    """Parse one markdown file with optional YAML front matter."""
    metadata: dict[str, Any] = {}
    body = text

    if text.lstrip().startswith(FRONT_MATTER_DELIMITER):
        stripped = text.lstrip()
        end = stripped.find(f"\n{FRONT_MATTER_DELIMITER}", len(FRONT_MATTER_DELIMITER))
        if end == -1:
            raise CorpusError(f"{fallback_id}: front matter is not terminated")
        raw = stripped[len(FRONT_MATTER_DELIMITER) : end]
        loaded = yaml.safe_load(raw) or {}
        if not isinstance(loaded, dict):
            raise CorpusError(f"{fallback_id}: front matter must be a mapping")
        metadata = loaded
        body = stripped[end + len(FRONT_MATTER_DELIMITER) + 1 :].lstrip("\n")

    known = {"doc_id", "title", "doc_type", "plan_ids", "effective_date", "url"}
    return Document(
        doc_id=str(metadata.get("doc_id", fallback_id)),
        title=str(metadata.get("title", fallback_id)),
        body=body,
        doc_type=str(metadata.get("doc_type", "policy")),
        plan_ids=[str(p) for p in metadata.get("plan_ids", []) or []],
        effective_date=_optional_str(metadata.get("effective_date")),
        url=_optional_str(metadata.get("url")),
        metadata={k: v for k, v in metadata.items() if k not in known},
    )


def load_corpus(root: Path | None = None) -> list[Document]:
    """Read every ``.md`` file under ``data/corpus`` (or ``root``)."""
    base = Path(root) if root is not None else DATA_ROOT / "corpus"
    if not base.exists():
        raise CorpusError(f"corpus directory not found: {base}")
    documents = [
        parse_document(path.read_text(encoding="utf-8"), path.stem)
        for path in sorted(base.rglob("*.md"))
    ]
    _assert_unique(documents)
    return documents


def build_indexes(
    documents: list[Document] | None = None,
    *,
    embedder: Embedder | None = None,
    root: Path | None = None,
) -> dict[str, SearchIndex]:
    """Chunk the corpus and load it into one in-memory index per domain."""
    docs = documents if documents is not None else load_corpus(root)
    indexes: dict[str, SearchIndex] = {
        "benefits": InMemoryHybridIndex(embedder),
        "policy": InMemoryHybridIndex(embedder),
    }
    grouped: dict[str, list[Document]] = {"benefits": [], "policy": []}
    for document in docs:
        target = INDEX_FOR_DOC_TYPE.get(document.doc_type, "policy")
        grouped[target].append(document)
    for name, group in grouped.items():
        indexes[name].add(chunk_documents(group))
    return indexes


def _assert_unique(documents: list[Document]) -> None:
    seen: set[str] = set()
    for document in documents:
        if document.doc_id in seen:
            raise CorpusError(f"duplicate doc_id in corpus: {document.doc_id}")
        seen.add(document.doc_id)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
