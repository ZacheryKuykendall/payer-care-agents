"""Corpus loading, chunking, hybrid retrieval and the Azure Search adapter."""

from __future__ import annotations

import pytest

from payer_agents.retrieval.azure_search import AzureAISearchIndex
from payer_agents.retrieval.chunking import chunk_document, split_long, split_sections
from payer_agents.retrieval.corpus import CorpusError, build_indexes, parse_document
from payer_agents.retrieval.embeddings import HashingEmbedder, cosine
from payer_agents.retrieval.memory_index import InMemoryHybridIndex
from payer_agents.retrieval.types import Chunk

MARKDOWN = """---
doc_id: test-doc
title: Test Plan Summary
doc_type: benefits
plan_ids: [PLAN-A]
effective_date: 2026-01-01
owner: Benefits
---

Intro paragraph before any heading.

## Costs

Specialist visit costs $35.

## Rules

A referral is required.
"""


# ----------------------------------------------------------------- parsing


def test_parse_document_reads_front_matter_and_keeps_extras_in_metadata():
    document = parse_document(MARKDOWN, "fallback")
    assert document.doc_id == "test-doc"
    assert document.doc_type == "benefits"
    assert document.plan_ids == ["PLAN-A"]
    assert document.metadata["owner"] == "Benefits"
    assert document.body.startswith("Intro paragraph")


def test_parse_document_without_front_matter_uses_the_filename():
    document = parse_document("# Title\n\nBody text here.", "my-file")
    assert document.doc_id == "my-file"
    assert document.doc_type == "policy"


@pytest.mark.parametrize(
    "text, message",
    [("---\ndoc_id: x\n", "not terminated"), ("---\n- a\n- b\n---\nbody", "must be a mapping")],
)
def test_parse_document_rejects_malformed_front_matter(text, message):
    with pytest.raises(CorpusError, match=message):
        parse_document(text, "bad")


def test_load_corpus_rejects_a_missing_directory(tmp_path):
    with pytest.raises(CorpusError, match="not found"):
        from payer_agents.retrieval.corpus import load_corpus

        load_corpus(tmp_path / "nope")


def test_build_indexes_rejects_duplicate_doc_ids(tmp_path):
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("---\ndoc_id: same\ntitle: t\n---\nbody\n", encoding="utf-8")
    with pytest.raises(CorpusError, match="duplicate doc_id"):
        build_indexes(root=tmp_path)


# ---------------------------------------------------------------- chunking


def test_split_sections_keeps_the_preamble_and_each_heading():
    sections = split_sections(parse_document(MARKDOWN, "x").body)
    assert [heading for heading, _ in sections] == ["Overview", "Costs", "Rules"]


def test_split_sections_handles_text_with_no_headings():
    assert split_sections("just prose") == [("Overview", "just prose")]
    assert split_sections("   ") == []


def test_split_long_breaks_on_paragraphs_and_keeps_an_overlap():
    text = "\n\n".join(["para one " * 20, "para two " * 20, "para three " * 20])
    pieces = split_long(text, max_chars=250, overlap=20)
    assert len(pieces) > 1
    assert all(len(piece) < 400 for piece in pieces)
    assert split_long("short", 100, 10) == ["short"]


def test_chunk_document_stamps_section_and_plan_onto_every_chunk():
    chunks = chunk_document(parse_document(MARKDOWN, "x"))
    assert len(chunks) == 3
    assert {c.section for c in chunks} == {"Overview", "Costs", "Rules"}
    assert all(c.plan_ids == ["PLAN-A"] for c in chunks)
    assert all(c.chunk_id.startswith("test-doc#") for c in chunks)


# ------------------------------------------------------------- filtering


def _chunk(chunk_id="c1", plan_ids=None, doc_type="benefits", text="specialist copay $35"):
    return Chunk(
        chunk_id=chunk_id,
        doc_id="d",
        title="T",
        section="S",
        text=text,
        doc_type=doc_type,
        plan_ids=plan_ids or [],
    )


def test_plan_filter_treats_an_empty_plan_list_as_applying_to_everyone():
    universal = _chunk(plan_ids=[])
    specific = _chunk(plan_ids=["PLAN-A"])
    assert universal.matches({"plan_id": "PLAN-B"}) is True
    assert specific.matches({"plan_id": "PLAN-B"}) is False
    assert specific.matches({"plan_id": "PLAN-A"}) is True


def test_filters_handle_none_lists_and_metadata_fallback():
    chunk = _chunk(doc_type="policy")
    assert chunk.matches(None) is True
    assert chunk.matches({"plan_id": None}) is True
    assert chunk.matches({"doc_type": ["policy", "benefits"]}) is True
    assert chunk.matches({"doc_type": "benefits"}) is False
    assert chunk.matches({"missing_field": "x"}) is False


# ------------------------------------------------------------- embeddings


def test_hashing_embedder_is_deterministic_and_unit_length():
    embedder = HashingEmbedder(dimensions=64)
    first = embedder.embed("specialist copay")
    assert first == embedder.embed("specialist copay")
    assert sum(v * v for v in first) == pytest.approx(1.0, abs=1e-9)
    assert embedder.embed("") == [0.0] * 64


def test_hashing_embedder_separates_unrelated_text():
    embedder = HashingEmbedder(dimensions=512)
    related = cosine(
        embedder.embed("specialist visit copay"), embedder.embed("copay specialist visit")
    )
    unrelated = cosine(
        embedder.embed("specialist visit copay"), embedder.embed("bariatric surgery criteria")
    )
    assert related > unrelated


def test_cosine_guards_degenerate_inputs():
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_embedder_rejects_a_tiny_dimension():
    with pytest.raises(ValueError):
        HashingEmbedder(dimensions=4)


# ------------------------------------------------------------ hybrid index


def test_index_returns_the_relevant_chunk_first():
    index = InMemoryHybridIndex()
    index.add(
        [
            _chunk("a", text="Specialist office visit costs a $35 copay."),
            _chunk("b", text="Hearing aids carry a $1,200 allowance per ear."),
            _chunk("c", text="Bariatric surgery requires documented weight history."),
        ]
    )
    hits = index.search("what is the specialist copay", top_k=2)
    assert hits[0].chunk.chunk_id == "a"
    # Chunks that match on neither signal are absent rather than ranked last. An
    # agent that pads its citations with irrelevant sources is harder to check.
    assert [hit.chunk.chunk_id for hit in hits] == ["a"]


def test_index_fuses_lexical_and_vector_signals():
    index = InMemoryHybridIndex()
    index.add([_chunk("a", text="Specialist office visit copay")])
    hit = index.search("specialist copay")[0]
    assert hit.lexical_score > 0
    assert hit.vector_score > 0
    assert hit.score > 0


def test_index_applies_filters_and_handles_empty_cases():
    index = InMemoryHybridIndex()
    index.add([_chunk("a", plan_ids=["PLAN-A"]), _chunk("b", plan_ids=["PLAN-B"])])
    assert [h.chunk.chunk_id for h in index.search("copay", filters={"plan_id": "PLAN-A"})] == ["a"]
    assert index.search("copay", top_k=0) == []
    assert InMemoryHybridIndex().search("anything") == []


def test_index_replaces_a_chunk_on_re_add_without_leaking_term_statistics():
    index = InMemoryHybridIndex()
    index.add([_chunk("a", text="original wording about dentures")])
    index.add([_chunk("a", text="replacement wording about eyeglasses")])
    assert len(index) == 1
    assert index.search("dentures") == []
    assert index.search("eyeglasses")[0].chunk.chunk_id == "a"


def test_hit_converts_to_a_citation_a_human_can_follow():
    index = InMemoryHybridIndex()
    index.add([_chunk("a")])
    citation = index.search("copay")[0].to_citation()
    assert citation.source_id == "a"
    assert citation.section == "S"
    assert citation.snippet


def test_real_corpus_retrieves_the_expected_policy(indexes):
    hits = indexes["policy"].search("criteria for MRI of the lumbar spine", top_k=3)
    assert any("mp-001" in hit.chunk.doc_id for hit in hits)


def test_real_corpus_scopes_benefits_to_the_members_plan(indexes):
    hits = indexes["benefits"].search(
        "specialist copay", top_k=5, filters={"plan_id": "NW-MA-HMO-2026"}
    )
    assert hits
    assert all(not hit.chunk.plan_ids or "NW-MA-HMO-2026" in hit.chunk.plan_ids for hit in hits)


# ------------------------------------------------------- azure search adapter


class _FakeSearchClient:
    def __init__(self):
        self.uploaded: list[dict] = []
        self.queries: list[dict] = []

    def merge_or_upload_documents(self, documents):
        self.uploaded.extend(documents)

    def search(self, **kwargs):
        self.queries.append(kwargs)
        return [
            {
                "chunk_id": "x#00-00",
                "doc_id": "x",
                "title": "Doc",
                "section": "Costs",
                "content": "Specialist copay $35",
                "doc_type": "benefits",
                "plan_ids": ["PLAN-A"],
                "@search.score": 3.5,
            }
        ]


def test_azure_adapter_uploads_vectors_alongside_content():
    fake = _FakeSearchClient()
    index = AzureAISearchIndex("https://x", "idx", HashingEmbedder(32), client=fake)
    index.add([_chunk("a")])
    assert fake.uploaded[0]["chunk_id"] == "a"
    assert len(fake.uploaded[0]["contentVector"]) == 32
    index.add([])
    assert len(fake.uploaded) == 1


def test_azure_adapter_returns_hits_in_the_portable_shape():
    fake = _FakeSearchClient()
    index = AzureAISearchIndex("https://x", "idx", HashingEmbedder(32), client=fake)
    hits = index.search("copay", top_k=2, filters={"doc_type": "benefits"})
    assert hits[0].chunk.title == "Doc"
    assert hits[0].score == pytest.approx(3.5)
    assert fake.queries[0]["query_type"] == "semantic"


@pytest.mark.parametrize(
    "filters, expected",
    [
        (None, None),
        ({}, None),
        ({"doc_type": None}, None),
        ({"doc_type": "policy"}, "doc_type eq 'policy'"),
        ({"doc_type": ["a", "b"]}, "(doc_type eq 'a' or doc_type eq 'b')"),
        ({"plan_id": "P-1"}, "(plan_ids/any(p: p eq 'P-1') or not plan_ids/any())"),
        ({"doc_type": "o'brien"}, "doc_type eq 'o''brien'"),
    ],
)
def test_odata_filter_translation(filters, expected):
    assert AzureAISearchIndex.build_filter(filters) == expected
