"""Matching: retrieve, extract, verify, compare."""

from __future__ import annotations

import pytest

from app.schemas.enums import RequirementStatus, VerificationOutcome
from app.services.match.match import build_query, match_requirement


class _NoRows:
    """A result that holds nothing: no product row, no fact, no page."""

    def first(self):
        return None

    def all(self):
        return []


class FakeSession:
    """A session that answers nothing, so the retrieval finds nothing and the
    fact cache misses - the corpus has no product row for a synthetic model.

    It counts commits because the matcher releases the transaction before
    asking the model, and a test can then say that it did.
    """

    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1

    def execute(self, statement):
        return _NoRows()


class _DeclaresCategory:
    """A session whose corpus holds documents, and they declare a category.

    `first()` answers nothing, so the product lookup and the fact cache both
    miss; `all()` is only ever asked by the category probe. The statements are
    kept so a test can say what the probe asked for.

    These are synthetic rows: the corpus this test reads is a list, not PDFs.
    """

    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.statements: list = []

    def commit(self) -> None:
        pass

    def execute(self, statement):
        self.statements.append(statement)
        rows = self.rows

        class _Answer:
            def first(self):
                return None

            def all(self):
                return rows

        return _Answer()


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
    turned into a missing capability (AGENTS #4), and it is never published
    unflagged: silence is a claim the verifier did not confirm (PRD 4)."""
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
    assert result.verification_outcome is VerificationOutcome.NOT_CHECKED
    assert result.review_required is True
    assert result.review_reason == "the passage does not state this capability"


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
    assert result.review_required is True


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


def test_a_pair_the_category_rule_dropped_is_reported_as_one(monkeypatch):
    """The corpus is not silent: its documents declare a product this clause is
    not about, and the item's category is what kept them out of the pair. That
    is our exclusion, so it is named apart from a gap (FR-17, D-135)."""
    from app.schemas.enums import ReasonCode
    from app.services.match import match

    monkeypatch.setattr(match, "search", lambda *a, **k: [])

    result = match_requirement(
        _DeclaresCategory([("pumps",)]),
        "Duty shall be 1500 RPM",
        category="valves",
        manufacturer_name="Synthetic Manufacturer",
        client=FakeClient({}),
    )

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.reason_code is ReasonCode.CATEGORY_MISMATCH
    assert result.review_required is True
    assert "valves" in result.review_reason
    assert "pumps" in result.review_reason
    assert result.llm_calls == 0


def test_the_category_probe_asks_for_the_model_the_clause_names(monkeypatch):
    """A clause about one model is not excluded by what the manufacturer's
    other products declare, so the probe is scoped the same way the search is."""
    from app.schemas.enums import ReasonCode
    from app.services.match import match

    monkeypatch.setattr(match, "search", lambda *a, **k: [])
    session = _DeclaresCategory([("pumps",)])

    result = match_requirement(
        session,
        "Duty shall be 1500 RPM",
        category="valves",
        manufacturer_name="Synthetic Manufacturer",
        model="SYNTHETIC-1",
        client=FakeClient({}),
    )

    probes = [str(s) for s in session.statements if "DISTINCT" in str(s)]
    assert probes, "the category probe never ran"
    assert "JOIN product" in probes[0]
    assert result.reason_code is ReasonCode.CATEGORY_MISMATCH


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


def test_a_page_with_no_text_layer_is_read_from_the_text_it_was_chunked_from(
    tmp_path, monkeypatch
):
    """A scanned datasheet page has no text layer at all, so the citation layer
    compared its quote with an empty string and failed it as "longer than the
    page" - every OCR page was unverifiable by construction (D-145)."""
    import pymupdf

    from types import SimpleNamespace

    from app.services.match import match

    path = tmp_path / "catalogue" / "acme" / "raw" / "scan.pdf"
    path.parent.mkdir(parents=True)
    document = pymupdf.open()
    document.new_page()  # an image page: nothing to read from the text layer
    document.save(path)
    document.close()
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path)
    )

    class _ScannedPageSession:
        """The document's path, then the OCR text its page's chunks hold."""

        def __init__(self, chunks):
            self.chunks = chunks

        def execute(self, *args, **kwargs):
            return self

        def first(self):
            return (str(path),)

        def all(self):
            return [(text,) for text in self.chunks]

    session = _ScannedPageSession(["Face to face: 122 mm", "End connection: NPT"])

    text = match._page_text(session, _candidate())

    assert text is not None
    assert "Face to face: 122 mm" in text
    assert "End connection: NPT" in text


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


def test_the_transaction_is_closed_before_the_model_is_asked(monkeypatch):
    """Reading the rows that lead here opens a transaction, and the model then
    runs for seconds. Left open, it holds its connection idle for every pair
    the pass works (D-132)."""
    from app.services.match import match
    from app.services.match.retrieve import Candidate

    monkeypatch.setattr(
        match,
        "search",
        lambda *a, **k: [
            Candidate(
                chunk_id=f"c{index}",
                document_id="d1",
                text=f" passage {index}",
                page=index,
                model="SYNTHETIC-1",
            )
            for index in (1, 2)
        ],
    )
    monkeypatch.setattr(match, "_page_text", lambda s, c: c.text)

    session = FakeSession()
    commits_at_call: list[int] = []

    class Watching(FakeClient):
        def complete_json(self, system, user, **kwargs):
            commits_at_call.append(session.commits)
            return {"requirements": []}

    match_requirement(session, "Body shall be SS316", client=Watching({}))

    # One release before each reading, not one for the whole requirement.
    assert commits_at_call == [1, 2]


def _cached_fact_row(**overrides) -> dict:
    """One `cached_fact` row, as the findings read returns it (synthetic)."""
    row = {
        "id": "fact-1",
        "value": "CF8M",
        "unit": None,
        "value_type": "material",
        "document_id": "d1",
        "page": 12,
        "quote": "1 | BODY | A216 WCB, WCC/ A352 LCB, LCC/A351 CF8M",
        "attribute": "material_grade",
        "is_family_level": False,
    }
    row.update(overrides)
    return row


def _passage_of(model: str, manufacturer: str = "Acme"):
    from app.services.match.retrieve import Candidate

    return Candidate(
        chunk_id="c1",
        document_id="d1",
        text="1 | BODY | A216 WCB, WCC/ A352 LCB, LCC/A351 CF8M",
        page=12,
        manufacturer=manufacturer,
        model=model,
    )


def test_a_product_whose_attribute_is_already_verified_costs_no_call(monkeypatch):
    """The reading is the expensive part, and the passage names the product it
    is about, so the cache is reachable inside a run and not only when the
    caller knew the model beforehand (FR-21, D-133)."""
    from app.services.match import match
    from app.services.match import findings

    monkeypatch.setattr(match, "search", lambda *a, **k: [_passage_of("SYNTHETIC-1")])
    monkeypatch.setattr(findings, "product_id_for", lambda s, name, model: "prod-1")
    monkeypatch.setattr(findings, "cached_fact", lambda *a, **k: _cached_fact_row())

    class Refuses(FakeClient):
        def complete_json(self, system, user, **kwargs):
            raise AssertionError("the model was asked for a fact already verified")

    result = match_requirement(
        FakeSession(),
        "Body shall be SS316",
        requirement_value="SS316",
        client=Refuses({}),
    )

    assert result.llm_calls == 0
    assert result.status is RequirementStatus.FULL_MATCH
    assert result.capability_text == "CF8M"
    assert result.page == 12
    # The comparison ran on the stored value, so the verdict is the run's own.
    assert "SS316" in result.reason
    assert "extract" not in result.stage_seconds
    assert result.stage_seconds["cached_fact"] >= 0.0


def test_the_cache_is_asked_for_the_products_the_passage_names(monkeypatch):
    """A fact about one product says nothing about another, so the key is the
    candidate's own manufacturer and model - and a different manufacturer is
    never answered from it."""
    from app.services.match import match
    from app.services.match.facts import FactResult

    keys: list[tuple] = []

    def record(session, text, manufacturer, model):
        keys.append((manufacturer, model))
        return None

    monkeypatch.setattr(match, "search", lambda *a, **k: [_passage_of("SYNTHETIC-1")])
    monkeypatch.setattr(match, "_cached_fact", record)
    monkeypatch.setattr(match, "_page_text", lambda s, c: c.text)
    monkeypatch.setattr(
        match,
        "extract_fact",
        lambda *a, **k: FactResult(facts=[]),
    )

    result = match_requirement(
        FakeSession(),
        "Body shall be SS316",
        client=FakeClient({"requirements": []}),
    )

    # The first key is the check made before retrieval, when the caller named no
    # product; the second is this passage's own product.
    assert keys == [(None, None), ("Acme", "SYNTHETIC-1")]
    # Nothing cached, so the passage was read: the miss pays as before.
    assert result.llm_calls == 1


def test_a_second_requirement_on_the_same_product_is_read_once(monkeypatch):
    """Two clauses asking one product for one attribute are a single reading and
    still two results: the assessments are never merged. The row the first
    answer writes through `store_match` is placed here, because the writer needs
    a database."""
    from app.services.match import match
    from app.services.match import findings

    stored: dict = {}

    monkeypatch.setattr(match, "search", lambda *a, **k: [_passage_of("SYNTHETIC-1")])
    monkeypatch.setattr(findings, "product_id_for", lambda s, name, model: "prod-1")
    monkeypatch.setattr(
        findings, "cached_fact", lambda s, product_id, term: stored.get(product_id)
    )
    monkeypatch.setattr(match, "_page_text", lambda s, c: c.text)

    client = FakeClient(
        {
            "requirements": [
                {
                    "found": True,
                    "attribute": "body material",
                    "value": "CF8M",
                    "unit": None,
                    "operator": "eq",
                    "model": "SYNTHETIC-1",
                    "quote": "1 | BODY | A216 WCB, WCC/ A352 LCB, LCC/A351 CF8M",
                }
            ]
        }
    )

    first = match_requirement(
        FakeSession(), "Body shall be SS316", requirement_value="SS316", client=client
    )
    stored["prod-1"] = _cached_fact_row()
    second = match_requirement(
        FakeSession(), "Body shall be SS316", requirement_value="SS316", client=client
    )

    assert first.llm_calls == 1
    assert "extract" in first.stage_seconds
    assert second.llm_calls == 0
    assert "extract" not in second.stage_seconds
    # The second clause is its own answer, reached the same way.
    assert second.status is first.status
    assert second.score == first.score


# --- A compound clause is not answered by one of its attributes (D-161) ------


_COMPOUND = (
    'Three piece ball valves 2" Socket weld ends Face to face distance 122mm '
    "ASME class 600 Cast Carbon steel"
)


def test_a_compound_clause_is_not_answered_by_one_of_its_attributes() -> None:
    """A BOQ title that fixes a class *and* a body material is two
    requirements. A full match on the class alone claims more than was
    checked, so it becomes a partial match for a person to look at (PRD 4,
    D-161). This clause is synthetic."""
    from app.services.match.match import MatchResult, _note_compound_clause

    result = MatchResult(
        requirement_text=_COMPOUND,
        status=RequirementStatus.FULL_MATCH,
        score=1.0,
        attribute="pressure class",
        reason="class 600 meets class 600",
    )
    noted = _note_compound_clause(result, _COMPOUND)

    assert noted.status is RequirementStatus.PARTIAL_MATCH
    assert noted.score <= 0.75
    assert noted.review_required is True
    assert "material_grade" in noted.review_reason
    assert "material_grade" in noted.reason
    # The original reason is kept, not replaced: the class did meet the class.
    assert noted.reason.startswith("class 600 meets class 600")


def test_a_single_attribute_clause_keeps_its_verdict() -> None:
    """One attribute fixed by the clause is a clause this comparison can
    answer. `Pressure class 150` must not be flagged (D-161)."""
    from app.services.match.match import MatchResult, _note_compound_clause

    result = MatchResult(
        requirement_text="Pressure class 150",
        status=RequirementStatus.FULL_MATCH,
        score=1.0,
        attribute="pressure class",
        reason="class 150 meets class 150",
    )
    noted = _note_compound_clause(result, "Pressure class 150")

    assert noted.status is RequirementStatus.FULL_MATCH
    assert noted.score == 1.0
    assert noted.review_required is False


def test_a_failing_value_on_a_compound_clause_is_flagged_not_upgraded() -> None:
    """A value that fails the requirement fails the clause whatever the other
    attributes say, so the status stays *missing* - but a person is told the
    reading covered one attribute of a compound clause (D-161)."""
    from app.services.match.match import MatchResult, _note_compound_clause

    result = MatchResult(
        requirement_text=_COMPOUND,
        status=RequirementStatus.MISSING_CAPABILITY,
        score=0.0,
        attribute="pressure class",
        reason="class 300 is below the required 600",
    )
    noted = _note_compound_clause(result, _COMPOUND)

    assert noted.status is RequirementStatus.MISSING_CAPABILITY
    assert noted.review_required is True
    assert noted.score == 0.0


def test_the_cache_path_marks_a_compound_clause_too() -> None:
    """The two wrong Flowserve pairs in the replay are answered from the fact
    cache, not by reading a passage, so the flag has to be applied there as
    well as on the fresh path (D-161). The fact row and clause are synthetic."""
    from app.services.match.match import _from_cached_fact

    result = _from_cached_fact(
        _cached_fact_row(attribute="pressure_class", value="600"),
        _COMPOUND,
        requirement_value="600",
        requirement_unit=None,
        requirement_operator="eq",
        manufacturer_name="Acme",
        model="SYNTHETIC-1",
    )

    assert result.status is RequirementStatus.PARTIAL_MATCH
    assert result.review_required is True
    assert "material_grade" in result.review_reason
