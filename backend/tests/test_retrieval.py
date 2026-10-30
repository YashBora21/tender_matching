"""Retrieval: the two searches, the merge, and the exact-token boost."""

from __future__ import annotations

import pytest

from app.services.retrieval import (
    Candidate,
    fuse,
    identifiers,
    keyword_search,
    semantic_search,
    search,
)


def _candidate(chunk_id: str, text: str = "") -> Candidate:
    return Candidate(chunk_id=chunk_id, document_id="d1", text=text)


def test_the_words_that_identif_a_product_are_found():
    """A model number, a standard code, a material grade and a size are the
    words that name a product (FR-19)."""
    found = identifiers("Body shall be SS316 to IS 1239, DN50, 16 bar")
    assert "ss316" in found
    assert "is 1239" in found or "is1239" in found
    assert "dn50" in found or "dn 50" in found


def test_a_value_is_also_found_on_its_own():
    """A datasheet table writes the value in the cell and the unit in the
    column header, so "267 mm" never appears and "267" does."""
    found = identifiers("Face to face distance 267mm")
    assert "267" in found


def test_the_two_searches_are_merged_by_rank():
    """Reciprocal rank fusion: a chunk both searches rank highly comes first
    (FR-18)."""
    keyword = [_candidate("a"), _candidate("b"), _candidate("c")]
    semantic = [_candidate("c"), _candidate("a")]

    merged = fuse(keyword, semantic)

    assert [c.chunk_id for c in merged][0] == "a"
    assert merged[0].score >= merged[-1].score
    # A chunk only one search found is still found.
    assert {c.chunk_id for c in merged} == {"a", "b", "c"}


def test_a_chunk_both_searches_found_scores_higher_than_one_only():
    keyword = [_candidate("a"), _candidate("b")]
    semantic = [_candidate("a")]

    merged = {c.chunk_id: c for c in fuse(keyword, semantic)}

    assert merged["a"].score > merged["b"].score


def test_the_search_needs_a_query():
    assert keyword_search(None, "") == []
    assert semantic_search(None, "") == []


def _sql(expression) -> str:
    """The condition as PostgreSQL would receive it, for a reader to check."""
    from sqlalchemy.dialects import postgresql

    return str(
        expression.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_a_requirement_without_a_category_is_not_filtered():
    from app.services.retrieval import _category_filter

    assert _category_filter(None) is None


def test_a_document_in_another_category_is_excluded():
    """A pump catalogue is not evidence for a gate valve (FR-17)."""
    from app.services.retrieval import _category_filter

    sql = _sql(_category_filter("valves"))

    # The other categories are excluded...
    assert "pumps" in sql
    assert "motors" in sql
    # ...and the one asked for is not.
    assert "valves" not in sql
    assert "IS NULL" in sql


def test_a_document_that_declares_no_category_is_kept():
    """A missing category is not a category that says "not this one".

    Older manifests wrote the manufacturer's slug into `category`, so a
    document can also declare something that is not a product category at all.
    Filtering on equality alone dropped the whole corpus and reported the
    silence as "no evidence found" (PRD 4).
    """
    from app.services.retrieval import _category_filter

    sql = _sql(_category_filter("circor"))

    assert "valves" in sql and "pumps" in sql and "motors" in sql
    assert "circor" not in sql
    assert "IS NULL" in sql


def test_a_chunk_that_names_a_different_valve_type_is_dropped(monkeypatch):
    """A gate-valve requirement is not answered by a ball-valve datasheet,
    even though both are `valves` (D-97)."""
    from app.services import retrieval

    silent = _candidate("silent", "Body shall be SS316")
    gate = _candidate("gate", "Gate valve body shall be SS316")
    ball = _candidate("ball", "Ball valve body shall be SS316")

    monkeypatch.setattr(
        retrieval, "keyword_search", lambda *a, **k: [silent, gate, ball]
    )
    monkeypatch.setattr(retrieval, "semantic_search", lambda *a, **k: [])

    kept = search(None, "gate valve", category="valves", product_type="gate")

    # The silent chunk is kept: silence is not a different type.
    assert [candidate.chunk_id for candidate in kept] == ["silent", "gate"]


def test_a_document_title_names_the_type_of_a_silent_chunk(monkeypatch):
    """The title is where a catalogue declares what it covers, and a chunk
    page rarely repeats it."""
    from app.services import retrieval

    ball = Candidate(
        chunk_id="ball",
        document_id="d1",
        text="Body shall be SS316",
        document_title="Ball-Valve-Catalog",
    )

    monkeypatch.setattr(retrieval, "keyword_search", lambda *a, **k: [ball])
    monkeypatch.setattr(retrieval, "semantic_search", lambda *a, **k: [])

    assert search(None, "gate valve", category="valves", product_type="gate") == []


def test_a_document_family_places_a_silent_chunk(monkeypatch):
    """A chunk that never says "ball valve" is still in a ball-valve
    catalogue, and the document's declared family says so (D-100)."""
    from app.services import retrieval

    ball = Candidate(
        chunk_id="ball",
        document_id="d1",
        text="Body shall be SS316",
        model_or_family="Series 7/8",
    )

    monkeypatch.setattr(retrieval, "keyword_search", lambda *a, **k: [ball])
    monkeypatch.setattr(retrieval, "semantic_search", lambda *a, **k: [])

    kept = search(None, "ball valve", category="valves", product_type="ball")

    assert [candidate.chunk_id for candidate in kept] == ["ball"]
    assert kept[0].document_type == "ball"
    assert kept[0].type_unconfirmed is False


def test_a_silent_chunk_in_a_different_type_document_is_dropped(monkeypatch):
    """A control-valve brochure cannot answer a gate-valve requirement just
    because one of its pages never repeats "control valve" (D-100)."""
    from app.services import retrieval

    control = Candidate(
        chunk_id="control",
        document_id="d1",
        text="Body shall be SS316",
        model_or_family="D-Series",
    )

    monkeypatch.setattr(retrieval, "keyword_search", lambda *a, **k: [control])
    monkeypatch.setattr(retrieval, "semantic_search", lambda *a, **k: [])

    assert search(None, "gate valve", category="valves", product_type="gate") == []


def test_a_document_with_no_declared_type_is_kept_but_flagged(monkeypatch):
    """When neither the chunk nor the document names a type, the evidence is
    kept and marked family-level for review, never dropped (PRD 4, D-100)."""
    from app.services import retrieval

    unknown = Candidate(
        chunk_id="unknown",
        document_id="d1",
        text="Body shall be SS316",
        document_title="WCENBR0001_EN_AQ.pdf",
    )

    monkeypatch.setattr(retrieval, "keyword_search", lambda *a, **k: [unknown])
    monkeypatch.setattr(retrieval, "semantic_search", lambda *a, **k: [])

    kept = search(None, "gate valve", category="valves", product_type="gate")

    assert [candidate.chunk_id for candidate in kept] == ["unknown"]
    assert kept[0].type_unconfirmed is True


def test_without_a_type_nothing_is_filtered(monkeypatch):
    from app.services import retrieval

    candidates = [
        _candidate("gate", "Gate valve"),
        _candidate("ball", "Ball valve"),
    ]
    monkeypatch.setattr(retrieval, "keyword_search", lambda *a, **k: candidates)
    monkeypatch.setattr(retrieval, "semantic_search", lambda *a, **k: [])

    kept = search(None, "valve", category="valves", product_type=None)

    assert [candidate.chunk_id for candidate in kept] == ["gate", "ball"]
