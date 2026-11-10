"""Synthetic identifiers and BOQ titles; no catalogue specifications."""
from app.services.tender.ingest import TenderItem, assign_to_items


def item(number, title):
    return TenderItem(item_id=number, title=title, description=title)


def row(part="SYN-123"):
    return {"part_number": part, "title": "misspelled synthetic title", "description": "synthetic source row"}


def test_unique_exact_part_binds_despite_title_difference():
    assigned, unresolved = assign_to_items([row()], [item("1", "SYN-123 synthetic BOQ"), item("2", "other")])
    assert not unresolved and assigned[0].item_id == "1"


def test_part_substring_does_not_bind_to_a_longer_identifier():
    assigned, unresolved = assign_to_items([row()], [item("1", "SYN-1234 synthetic BOQ"), item("2", "other")])
    assert not assigned and unresolved


def test_duplicate_part_across_items_is_not_guessed():
    assigned, unresolved = assign_to_items([row()], [item("1", "SYN-123 first"), item("2", "SYN-123 second")])
    assert not assigned and unresolved


def test_serial_and_part_disagreement_is_not_guessed():
    source = {**row(), "number": "2"}
    assigned, unresolved = assign_to_items([source], [item("1", "SYN-123 first"), item("2", "other")])
    assert not assigned and unresolved


def test_retyping_preserves_unresolved_source_assignment_reason():
    from app.services.tender.ingest import RequirementRecord
    from app.services.requirement.retype import retype
    from app.schemas.enums import RequirementType
    source = RequirementRecord(text="synthetic cylinder head cover", item_id="",
                               requirement_type=RequirementType.UNKNOWN,
                               review_required=True, review_reason="synthetic conflicting source identifiers")
    assert not retype(source)
    assert source.requirement_type is RequirementType.UNKNOWN
    assert "synthetic conflicting source identifiers" in source.review_reason
