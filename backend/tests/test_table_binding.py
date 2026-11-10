"""Synthetic table columns: a real value under another header is not evidence."""
from app.services.match.verify import check_table_binding


def test_wrong_column_is_rejected_despite_value_and_attribute_on_page():
    tables = [[["BodyDesign", "Face to Face", "EndConnections"],
               ["Integralflange", "Raised Face", "ASME B16.5"]]]
    result = check_table_binding(tables, "end connection", "integral flange")
    assert result is not None and not result.passed


def test_value_in_requested_column_is_not_rejected():
    tables = [[["Body Design", "End Connections"], ["Synthetic design", "Flanged"]]]
    assert check_table_binding(tables, "end connection", "flanged") is None


def test_no_table_is_not_a_column_failure():
    assert check_table_binding([], "end connection", "flanged") is None


def test_wrong_column_never_publishes_missing_capability(monkeypatch):
    from types import SimpleNamespace
    from app.services.match import match
    from app.services.match.facts import FactResult, ExtractedFact
    from app.services.match.retrieve import Candidate

    monkeypatch.setattr(match, "_cached_fact", lambda *a: None)
    monkeypatch.setattr(match, "_page_text", lambda *a: "End Connections Integral flange")
    monkeypatch.setattr(match, "_source_tables", lambda *a: [
        [["Body Design", "End Connections"], ["Integral flange", "ASME B16.5"]]])
    monkeypatch.setattr(match, "_evidence_context", lambda *a: {'tables':[
        [["Body Design", "End Connections"], ["Integral flange", "ASME B16.5"]]]})
    result = match._match_candidate(
        SimpleNamespace(),
        Candidate(chunk_id="synthetic", document_id="synthetic", text="End Connections Integral flange",
                  manufacturer="Synthetic", model="Synthetic", page=1),
        "End connection shall be flanged", requirement_value="flanged",
        extracted=FactResult(facts=[ExtractedFact(found=True, attribute="end connection",
            value="integral flange", quote="End Connections Integral flange", operator="eq")]))
    assert result.status.value == "no_evidence_found"
    assert result.verification_outcome.value == "failed"
    assert result.review_required
