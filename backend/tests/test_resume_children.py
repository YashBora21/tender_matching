"""Synthetic source rows; no provider or application database access."""
from dataclasses import asdict

from app.schemas.enums import RequirementType
from app.services.llm import LLMBudgetExceeded
from app.services.tender.ingest import RequirementRecord
from app.services.tender.pipeline import _extract_all, _stored_records, to_stored


def siblings(item):
    source = f'Synthetic record {item}: nominal size 6 inches; operating pressure 8 bar'
    common = dict(item_id=str(item), page=1, source_table_id='synthetic-table',
                  source_row_id=str(item), source_text=source, source_role='boq')
    rows = [RequirementRecord(text=f'Synthetic record {item}', context_only=True,
                             requirement_type=RequirementType.INFORMATIONAL, **common)]
    for attribute, value, unit, quote in [('nominal_size', '6', 'inch', '6 inches'),
                                          ('operating_pressure', '8', 'bar', '8 bar')]:
        start = source.index(quote)
        rows.append(RequirementRecord(text=quote, attribute=attribute, value=value, unit=unit,
                    source_span=(start, start + len(quote)), **common))
    return rows


def test_resume_keeps_all_18_technical_children_even_when_context_is_first():
    rows = [row for item in range(1, 10) for row in siblings(item)]
    stored = [dict(asdict(to_stored(row, 'synthetic', model='synthetic')),
                   type=row.requirement_type.value) for row in rows]
    class Repo:
        def list_requirements(self, _): return stored
    class Forbidden:
        def complete_json(self, *args, **kwargs): raise AssertionError('Completed children need no new model call')
    resumed = _stored_records(Repo(), 'synthetic')
    assert sum(row.requirement_type is RequirementType.TECHNICAL for row in resumed) == 18
    output, calls, _, limited, _ = _extract_all(resumed, client=Forbidden(), preserve_extracted=True)
    assert sum(row.requirement_type is RequirementType.TECHNICAL for row in output) == 18
    assert len(output) == 27 and sum(row.context_only for row in output) == 9
    assert {row.item_id for row in output} == {str(i) for i in range(1, 10)}
    assert not calls and not limited
    assert all(row.source_row_id == row.item_id for row in output)


def test_budget_stop_preserves_every_child_of_incomplete_source_and_following_items():
    rows = siblings('first') + siblings('second')
    rows[2].source_span = None
    rows[2].attribute = None
    class Limited:
        def complete_json(self, *args, **kwargs): raise LLMBudgetExceeded('Synthetic budget')
    output, _, _, limited, _ = _extract_all(rows, client=Limited())
    assert limited and len(output) == len(rows)
    assert [(row.item_id, row.text) for row in output] == [(row.item_id, row.text) for row in rows]


def test_partial_source_error_cannot_displace_existing_technical_sibling():
    rows = siblings('first')
    rows[2].source_span = None
    rows[2].attribute = None
    class Empty:
        def complete_json(self, *args, **kwargs): return {'requirements': []}
    output, _, _, _, _ = _extract_all(rows, client=Empty(), preserve_extracted=True)
    assert any(row.attribute == 'nominal_size' and row.value == '6' for row in output)
    assert any(row.context_only for row in output)
