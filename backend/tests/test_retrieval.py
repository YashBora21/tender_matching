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
