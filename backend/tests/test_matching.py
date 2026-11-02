"""Matching: retrieve, extract, verify, compare."""

from __future__ import annotations

import pytest

from app.schemas.enums import RequirementStatus, VerificationOutcome
from app.services.match.match import build_query, match_requirement


class FakeSession:
    """A session that answers nothing, so the retrieval finds nothing."""


class FakeClient:
    """A client that answers with what it is given."""

    def __init__(self, answer):
        self.answer = answer

    def complete_json(self, system, user, **kwargs):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_a_requirement_nothing_answers_is_no_evidence_found(monkeypatch):
    """No evidence is not a missing capability (PRD 4)."""
    from app.services.match import match

    monkeypatch.setattr(match, "search", lambda *a, **k: [])

    result = match_requirement(FakeSession(), "Body shall be SS316", client=FakeClient({}))

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.score == 0.0
    assert result.review_required is True
    assert result.reason


def _passage_candidate(passage: str):
    from app.services.match.retrieve import Candidate

    return Candidate(
        chunk_id="c1",
        document_id="d1",
        text=passage,
        page=2,
        manufacturer="Synthetic Manufacturer",
        model="SYNTHETIC-1",
    )


def test_a_listed_set_that_excludes_the_requirement_is_missing_capability(monkeypatch):
    """The document offers flanged or butt weld ends, not screwed. That is a
    documented failing value, not silence (PRD 4)."""
    from app.services.match import match
    from app.services.match.facts import FactResult

    passage = "Body Style: Flanged end / Butt weld end"
    monkeypatch.setattr(
        match, "search", lambda *a, **k: [_passage_candidate(passage)]
    )
    monkeypatch.setattr(match, "_page_text", lambda *a, **k: passage)
    monkeypatch.setattr(
        match, "extract_fact", lambda *a, **k: FactResult(facts=[])
    )

    result = match_requirement(
        FakeSession(),
        'Three piece ball valves 2" Screwed end ASME class 150',
        client=FakeClient({}),
    )

    assert result.status is RequirementStatus.MISSING_CAPABILITY
    assert result.quote == passage
    assert result.verification_outcome is VerificationOutcome.PASSED


def test_a_silent_passage_stays_no_evidence_found(monkeypatch):
    """A page that does not mention the attribute is silent. It is never
    turned into a missing capability (AGENTS #4)."""
    from app.services.match import match
    from app.services.match.facts import FactResult

    passage = "Maximum operating temperature is 60 degC."
    monkeypatch.setattr(
        match, "search", lambda *a, **k: [_passage_candidate(passage)]
    )
    monkeypatch.setattr(match, "_page_text", lambda *a, **k: passage)
    monkeypatch.setattr(
        match, "extract_fact", lambda *a, **k: FactResult(facts=[])
    )

    result = match_requirement(
        FakeSession(),
        'Three piece ball valves 2" Screwed end ASME class 150',
        client=FakeClient({}),
    )

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND


# --- the retrieval query (D-96) --------------------------------------------


def test_the_query_carries_the_item_the_clause_and_what_was_read() -> None:
    query = build_query(
        "GATE VALVE, SIZE-150 MM, CLASS-150",
        "Flanged Gate valve class 150",
        attribute="pressure_class",
        value="CLASS150",
    )
    assert "GATE VALVE" in query
    assert "Flanged Gate valve class 150" in query
    # The canonical term is spelled the way a document spells it.
    assert "pressure class" in query
    assert "CLASS150" in query


def test_a_query_part_is_added_once() -> None:
    """The item title and the clause repeat each other; a repeated part would
    weight the same words twice in the keyword rank for no reason."""
    query = build_query("Gate valve class 150", "Gate valve class 150")
    assert query == "Gate valve class 150"


def test_a_bare_clause_is_searched_with_its_item_and_attribute(monkeypatch):
    """The stored clause may be "1500 RPM". Searching for it alone matches
    every page that mentions a speed; the item names the pump (D-96)."""
    from app.services.match import match

    seen: dict = {}

    def search(session, text, **kwargs):
        seen["text"] = text
        return []

    monkeypatch.setattr(match, "search", search)

    match_requirement(
        FakeSession(),
        "1500 RPM",
        item_title="CENTRIFUGAL PUMP 50 M3/HR",
        requirement_attribute="rotational_speed",
        requirement_value="1500",
        requirement_unit="rpm",
        client=FakeClient({}),
    )

    assert "CENTRIFUGAL PUMP" in seen["text"]
    assert "rotational speed" in seen["text"]
    assert "1500" in seen["text"]
    assert seen["text"] != "1500 RPM"


def test_the_item_type_is_sent_to_the_search(monkeypatch):
    """The category is `valves`; the type inside it is read from the item's
    title and passed to retrieval (D-97)."""
    from app.services.match import match

    seen: dict = {}

    def search(session, text, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(match, "search", search)

    match_requirement(
        FakeSession(),
        "Body shall be SS316",
        item_title="GATE VALVE, SIZE-150 MM",
        category="valves",
        client=FakeClient({}),
    )

    assert seen["product_type"] == "gate"


def test_an_ambiguous_item_type_is_not_filtered_on_a_guess(monkeypatch):
    from app.services.match import match

    seen: dict = {}

    def search(session, text, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(match, "search", search)

    match_requirement(
        FakeSession(),
        "Body shall be SS316",
        item_title="GATE VALVE AND BALL VALVE",
        category="valves",
        client=FakeClient({}),
    )

    assert seen["product_type"] is None


def test_a_passage_that_states_nothing_is_no_evidence_found(monkeypatch):
    from app.services.match import match
    from app.services.match.retrieve import Candidate

    candidate = Candidate(
        chunk_id="c1", document_id="d1", text="A gearbox for the pump", page=1
    )
    monkeypatch.setattr(match, "search", lambda *a, **k: [candidate])
    monkeypatch.setattr(match, "_page_text", lambda s, c: "A gearbox for the pump")

    client = FakeClient(
        {
            "requirements": [
                {
                    "found": False,
                    "attribute": None,
                    "value": None,
                    "unit": None,
                    "operator": None,
                    "model": None,
                    "quote": None,
                }
            ]
        }
    )

    result = match_requirement(FakeSession(), "Body shall be SS316", client=client)

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND


def test_a_quote_that_is_not_on_the_page_fails_the_citation_layer(monkeypatch):
    """A claim whose quote is not on the page it names is not verified
    (FR-23)."""
    from app.services.match import match
    from app.services.match.retrieve import Candidate

    candidate = Candidate(
        chunk_id="c1", document_id="d1", text="Body of body shall be SS316", page=1
    )
    monkeypatch.setattr(match, "search", lambda *a, **k: [candidate])
    # The page says something else.
    monkeypatch.setattr(match, "_page_text", lambda s, c: "A completely different page")

    client = FakeClient(
        {
            "requirements": [
                {
                    "found": True,
                    "attribute": "body material",
                    "value": "SS316",
                    "unit": None,
                    "operator": "eq",
                    "model": None,
                    "quote": "Body of body shall be SS316",
                }
            ]
        }
    )

    result = match_requirement(FakeSession(), "Body shall be SS316", client=client)

    # An unverified quote proves nothing about the manufacturer: it is not a
    # missing capability, it is an absence of evidence (D14).
    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.review_required is True
    assert "could not be verified" in (result.reason or "")
    assert result.quote == "Body of body shall be SS316"


def test_a_range_clause_is_compared_with_the_pair_it_states(monkeypatch):
    """"Between 10 and 20 m3/h", answered by a document that states 15.

    The clause keeps its value as the text it was written in ("10-20"), and the
    comparison was handed `None` for the pair, so a document that answered the
    requirement published *no evidence found* - the registry's range rule was
    never reachable from a run (D-121).
    """
    from app.services.match import match
    from app.services.match.retrieve import Candidate

    passage = "Rated flow 15 m3/h at 8 bar."
    candidate = Candidate(
        chunk_id="c1", document_id="d1", text=passage, page=1, model="SYNTHETIC-1"
    )
    monkeypatch.setattr(match, "search", lambda *a, **k: [candidate])
    monkeypatch.setattr(match, "_page_text", lambda s, c: passage)
    client = FakeClient(
        {
            "requirements": [
                {
                    "found": True,
                    "attribute": "flow rate",
                    "value": "15",
                    "unit": "m**3/hour",
                    "operator": "eq",
                    "model": None,
                    "quote": "Rated flow 15 m3/h at 8 bar.",
                }
            ]
        }
    )

    result = match_requirement(
        FakeSession(),
        "Flow rate between 10 and 20 m3/h",
        requirement_value="10-20",
        requirement_unit="m**3/hour",
        requirement_operator="between",
        client=client,
    )

    assert result.status is RequirementStatus.FULL_MATCH
    assert "within 10-20" in result.reason


def test_a_page_that_does_not_repeat_the_manufacturer_is_still_that_manufacturers_evidence(
    monkeypatch,
):
    """The entity layer reads the document row, not the passage (D-94).

    A CIRCOR brochure page that says only "body shall be SS316" used to fail
    the entity layer because the word CIRCOR was not on the page.
    """
    from app.services.match import match
    from app.services.match.retrieve import Candidate

    candidate = Candidate(
        chunk_id="c1",
        document_id="d1",
        text="Body of body shall be SS316",
        page=1,
        manufacturer="Acme Valves",
        model="GV-100",
    )
    monkeypatch.setattr(match, "search", lambda *a, **k: [candidate])
    monkeypatch.setattr(match, "_page_text", lambda s, c: candidate.text)

    client = FakeClient(
        {
            "requirements": [
                {
                    "found": True,
                    "attribute": "body material",
                    "value": "SS316",
                    "unit": None,
                    "operator": "eq",
                    "model": None,
                    "quote": "Body of body shall be SS316",
                }
            ]
        }
    )

    result = match_requirement(
        FakeSession(),
        "Body shall be SS316",
        requirement_value="SS316",
        requirement_operator="eq",
        manufacturer_name="Acme Valves",
        client=client,
    )

    assert result.status is RequirementStatus.FULL_MATCH
    assert result.verification_outcome.value == "passed"


def test_a_pressure_class_uses_its_ordered_evaluator(monkeypatch):
    """The model answers "class", the registry has a rule for the canonical
    term `pressure_class`, and Class 300 against a Class 150 requirement is a
    partial match that goes to review (D-95, D-99)."""
    from app.services.match import match
    from app.services.match.retrieve import Candidate

    candidate = Candidate(
        chunk_id="c1",
        document_id="d1",
        text="Valve is available in Class 150, 300 and 600",
        page=1,
        manufacturer="Acme Valves",
        model="GV-100",
    )
    monkeypatch.setattr(match, "search", lambda *a, **k: [candidate])
    monkeypatch.setattr(match, "_page_text", lambda s, c: candidate.text)

    client = FakeClient(
        {
            "requirements": [
                {
                    "found": True,
                    "attribute": "class",
                    "value": "300",
                    "unit": None,
                    "operator": "eq",
                    "model": None,
                    "quote": "Valve is available in Class 150, 300 and 600",
                }
            ]
        }
    )

    result = match_requirement(
        FakeSession(),
        "Flanged Gate valve class 150",
        requirement_value="150",
        requirement_operator="eq",
        manufacturer_name="Acme Valves",
        client=client,
    )

    assert result.status is RequirementStatus.PARTIAL_MATCH
    assert result.value_type is not None
    assert result.review_required is True


def test_an_extraction_that_fails_is_flagged_not_silently_empty(monkeypatch):
    """A failed extraction is a failure the reviewer can see."""
    from app.services.match import match
    from app.services.llm import LLMError
    from app.services.match.retrieve import Candidate

    candidate = Candidate(
        chunk_id="c1", document_id="d1", text="Body of body shall be SS316", page=1
    )
    monkeypatch.setattr(match, "search", lambda *a, **k: [candidate])

    result = match_requirement(
        FakeSession(), "Body shall be SS316", client=FakeClient(LLMError("not JSON"))
    )

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.review_required is True
    assert result.reason


def test_a_rate_limit_stops_the_match_instead_of_reading_as_no_evidence(monkeypatch):
    """A 429 is not the corpus being silent. It leaves the matcher so the run
    can record `rate_limited` (D5)."""
    import pytest

    from app.services.match import match
    from app.services.llm import LLMRateLimited
    from app.services.match.retrieve import Candidate

    candidate = Candidate(
        chunk_id="c1", document_id="d1", text="Body of body shall be SS316", page=1
    )
    monkeypatch.setattr(match, "search", lambda *a, **k: [candidate])

    with pytest.raises(LLMRateLimited):
        match_requirement(
            FakeSession(),
            "Body shall be SS316",
            client=FakeClient(LLMRateLimited("the model is rate limited")),
        )


def test_the_fact_that_answers_the_requirement_is_the_one_compared(monkeypatch):
    """One passage can state several capabilities. The comparison runs on the
    one that answers the requirement, not on whichever the model listed
    first."""
    from app.services.match import match
    from app.services.match.retrieve import Candidate

    candidate = Candidate(
        chunk_id="c1", document_id="d1", text="Body of body shall be SS316", page=1
    )
    monkeypatch.setattr(match, "search", lambda *a, **k: [candidate])
    monkeypatch.setattr(match, "_page_text", lambda s, c: candidate.text)

    client = FakeClient(
        {
            "requirements": [
                {"found": False, "attribute": None, "value": None, "unit": None,
                 "operator": None, "model": None, "quote": None},
                {"found": True, "attribute": "body material", "value": "SS316",
                 "unit": None, "operator": "eq", "model": None,
                 "quote": "Body of body shall be SS316"},
            ]
        }
    )

    result = match_requirement(
        FakeSession(),
        "Body shall be SS316",
        requirement_value="SS316",
        requirement_operator="eq",
        client=client,
    )

    assert result.fact_value == "SS316"
    assert result.status is RequirementStatus.FULL_MATCH


def test_family_level_evidence_is_capped_and_flagged_not_a_confirmed_match(monkeypatch):
    """A catalogue that covers a family is not evidence confirmed for one
    model: the score is capped and the claim goes to review (PRD 4)."""
    from app.schemas.enums import Applicability
    from app.services.match import match
    from app.services.match.retrieve import Candidate

    candidate = Candidate(
        chunk_id="c1",
        document_id="d1",
        text="Body of body shall be SS316",
        page=1,
        is_family_level=True,
    )
    monkeypatch.setattr(match, "search", lambda *a, **k: [candidate])
    monkeypatch.setattr(match, "_page_text", lambda s, c: candidate.text)

    client = FakeClient(
        {
            "requirements": [
                {"found": True, "attribute": "body material", "value": "SS316",
                 "unit": None, "operator": "eq", "model": None,
                 "quote": "Body of body shall be SS316"}
            ]
        }
    )

    result = match_requirement(
        FakeSession(),
        "Body shall be SS316",
        requirement_value="SS316",
        requirement_operator="eq",
        client=client,
    )

    assert result.applicability is Applicability.FAMILY_LEVEL
    assert result.score <= 0.5
    assert result.review_required is True


def test_evidence_from_a_document_with_no_declared_type_is_family_level(monkeypatch):
    """A document that never says which product type it covers may be about a
    different product. The claim is kept, but it is not model-level evidence
    and its score is capped (D-100)."""
    from app.schemas.enums import Applicability
    from app.services.match import match
    from app.services.match.retrieve import Candidate

    candidate = Candidate(
        chunk_id="c1",
        document_id="d1",
        text="Body of body shall be SS316",
        page=1,
        type_unconfirmed=True,
    )
    monkeypatch.setattr(match, "search", lambda *a, **k: [candidate])
    monkeypatch.setattr(match, "_page_text", lambda s, c: candidate.text)

    client = FakeClient(
        {
            "requirements": [
                {"found": True, "attribute": "body material", "value": "SS316",
                 "unit": None, "operator": "eq", "model": None,
                 "quote": "Body of body shall be SS316"}
            ]
        }
    )

    result = match_requirement(
        FakeSession(),
        "Body shall be SS316",
        requirement_value="SS316",
        requirement_operator="eq",
        client=client,
    )

    assert result.applicability is Applicability.FAMILY_LEVEL
    assert result.score <= 0.5
    assert result.review_required is True
    assert "does not declare its product type" in (result.review_reason or "")


def test_an_unmapped_attribute_has_its_own_reason_code(monkeypatch):
    """'We could not evaluate this attribute' is not 'the catalogue is
    silent'. Both keep the status *no evidence found*, and the code is what
    tells them apart (D-101)."""
    from app.schemas.enums import ReasonCode, VerificationOutcome
    from app.services.match import match
    from app.services.match.retrieve import Candidate

    candidate = Candidate(
        chunk_id="c1",
        document_id="d1",
        text="Wibble factor is 12",
        page=1,
    )
    monkeypatch.setattr(match, "search", lambda *a, **k: [candidate])
    monkeypatch.setattr(match, "_page_text", lambda s, c: candidate.text)

    client = FakeClient(
        {
            "requirements": [
                {"found": True, "attribute": "wibble factor", "value": "12",
                 "unit": None, "operator": "eq", "model": None,
                 "quote": "Wibble factor is 12"}
            ]
        }
    )

    result = match_requirement(
        FakeSession(), "Wibble factor shall be 12", client=client
    )

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.reason_code is ReasonCode.ATTRIBUTE_NOT_MAPPED
    assert result.verification_outcome is VerificationOutcome.NOT_CHECKED
    assert result.review_required is True


def test_a_silent_corpus_has_no_reason_code(monkeypatch):
    """Nothing was found, so no limit of ours stopped a comparison: the code
    stays empty, which is how a report counts silence apart from a gap in our
    own evaluation (D-101)."""
    from app.services.match import match

    monkeypatch.setattr(match, "search", lambda *a, **k: [])

    result = match_requirement(
        FakeSession(), "Body shall be SS316", client=FakeClient({})
    )

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.reason_code is None


def test_a_verified_fact_is_answered_without_retrieving_again(monkeypatch):
    """The structured fact cache is checked before retrieval (FR-21)."""
    from app.services.match import match

    def no_search(*args, **kwargs):  # pragma: no cover
        raise AssertionError("retrieval ran despite a cached fact")

    monkeypatch.setattr(match, "_cached_fact", lambda *a, **k: {
        "id": "f1",
        "value": "SS316",
        "unit": None,
        "value_type": "material",
        "document_id": "d1",
        "page": 4,
        "quote": "Body of body shall be SS316",
        "attribute": "body material",
        "is_family_level": False,
    })
    monkeypatch.setattr(match, "search", no_search)

    result = match_requirement(
        FakeSession(),
        "Body shall be SS316",
        requirement_value="SS316",
        requirement_operator="eq",
        manufacturer_name="Acme",
        model="Series 75",
    )

    assert result.llm_calls == 0
    assert result.page == 4
    assert result.quote == "Body of body shall be SS316"
    assert result.status is RequirementStatus.FULL_MATCH

# --- how many passages are read (D19) -------------------------------------


def _full_match_answer(text="Body of body shall be SS316"):
    return {
        "requirements": [
            {
                "found": True,
                "attribute": "body material",
                "value": "SS316",
                "unit": None,
                "operator": "eq",
                "model": None,
                "quote": text,
            }
        ]
    }


def test_a_verified_full_match_stops_the_search(monkeypatch):
    """Nothing a further passage says can beat a verified full match, so the
    remaining passages are not paid for (D19)."""
    from app.services.match import match
    from app.services.match.retrieve import Candidate

    candidates = [
        Candidate(
            chunk_id=f"c{index}",
            document_id="d1",
            text="Body of body shall be SS316",
            page=1,
        )
        for index in range(3)
    ]
    monkeypatch.setattr(match, "search", lambda *a, **k: list(candidates))
    monkeypatch.setattr(match, "_page_text", lambda s, c: c.text)

    calls = []

    class CountingClient:
        def complete_json(self, system, user, **kwargs):
            calls.append(user)
            return _full_match_answer()

    result = match_requirement(
        FakeSession(),
        "Body shall be SS316",
        requirement_value="SS316",
        requirement_operator="eq",
        client=CountingClient(),
    )

    assert result.status is RequirementStatus.FULL_MATCH
    assert len(calls) == 1
    assert result.llm_calls == 1
    # How many passages were available is still reported: the cap is not a
    # search that silently saw less.
    assert result.candidates == 3


def test_the_number_of_passages_read_comes_from_the_settings(monkeypatch):
    """The cost ceiling of a match is configuration, not a literal (D19)."""
    from types import SimpleNamespace

    from app.services.match import match

    seen: dict = {}

    def search(session, text, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(match, "search", search)
    monkeypatch.setattr(
        match, "get_settings", lambda: SimpleNamespace(match_max_candidates=4)
    )

    match_requirement(FakeSession(), "Body shall be SS316", client=FakeClient({}))

    assert seen["limit"] == 4

    # A caller can ask for a tighter cap; it is not silently replaced.
    match_requirement(
        FakeSession(), "Body shall be SS316", limit=2, client=FakeClient({})
    )

    assert seen["limit"] == 2


# --- the page a passage came from (the citation check) --------------------


class _PathRow:
    """The one column `_page_text` selects."""

    def __init__(self, value):
        self.value = value

    def first(self):
        return (self.value,)


class _DocumentSession:
    """A session whose only answer is one document's stored path."""

    def __init__(self, path):
        self.path = path

    def execute(self, *args, **kwargs):
        return _PathRow(self.path)


def _corpus_pdf(root, relative):
    import pymupdf

    path = root / "catalogue" / "acme" / "raw" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 90), "Body: austenitic stainless steel SS316", fontsize=11)
    document.save(path)
    document.close()
    return path


def _candidate(page=1):
    from app.services.match.retrieve import Candidate

    return Candidate(chunk_id="c1", document_id="d1", text="Body: SS316", page=page)


def test_a_relative_document_path_is_found_in_the_catalogue(tmp_path, monkeypatch):
    """The matching stage reads the page a quote claims, and that read used to
    crash on a missing import (D13). A path that is only a file name is looked
    for in the catalogue as well as the collector's tree."""
    from types import SimpleNamespace

    from app.services.match import match

    _corpus_pdf(tmp_path, "datasheet.pdf")
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path)
    )

    text = match._page_text(_DocumentSession("datasheet.pdf"), _candidate())

    assert text is not None
    assert "SS316" in text


def test_an_absolute_document_path_is_read_as_it_is(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.services.match import match

    path = _corpus_pdf(tmp_path, "datasheet.pdf")
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path)
    )

    text = match._page_text(_DocumentSession(str(path)), _candidate())

    assert text is not None
    assert "SS316" in text


def test_a_document_that_is_not_on_disk_has_no_page_text(tmp_path, monkeypatch):
    """No text means no citation check, so the quote stays unverified rather
    than being accepted on the model's word."""
    from types import SimpleNamespace

    from app.services.match import match

    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path)
    )

    assert match._page_text(_DocumentSession("gone.pdf"), _candidate()) is None


def test_a_matched_pair_records_the_time_of_each_stage(monkeypatch):
    """The seconds are measured where they are spent, not guessed afterwards (D-131)."""
    from app.services.match import match
    from app.services.match.retrieve import Candidate

    passage = "Rated flow 15 m3/h at 8 bar."
    candidate = Candidate(
        chunk_id="c1", document_id="d1", text=passage, page=1, model="SYNTHETIC-1"
    )
    monkeypatch.setattr(match, "search", lambda *a, **k: [candidate])
    monkeypatch.setattr(match, "_page_text", lambda s, c: passage)

    result = match_requirement(
        FakeSession(),
        "Flow rate 15 m3/h",
        requirement_value="15",
        requirement_unit="m**3/hour",
        requirement_operator="eq",
        client=FakeClient(
            {
                "requirements": [
                    {
                        "found": True,
                        "attribute": "flow rate",
                        "value": "15",
                        "unit": "m**3/hour",
                        "operator": "eq",
                        "model": None,
                        "quote": "Rated flow 15 m3/h at 8 bar.",
                    }
                ]
            }
        ),
    )

    assert result.status is RequirementStatus.FULL_MATCH
    assert {"retrieve", "extract", "page_read", "verify", "compare"} <= set(
        result.stage_seconds
    )
    assert all(seconds >= 0.0 for seconds in result.stage_seconds.values())


def test_a_requirement_nothing_retrieves_costs_only_the_search(monkeypatch):
    """A pair that never reaches the model has no `extract` to report (D-131)."""
    from app.services.match import match

    monkeypatch.setattr(match, "search", lambda *a, **k: [])

    result = match_requirement(FakeSession(), "Body shall be SS316", client=FakeClient({}))

    assert set(result.stage_seconds) == {"cached_fact", "retrieve"}
