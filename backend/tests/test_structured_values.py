"""Synthetic source values: normalization never edits source evidence."""
import pytest

from app.services.requirement.structured_values import normalize_fields, resolve_name
from app.comparison.registry import ComparisonRequest, evaluate
from app.schemas.enums import Operator, RequirementStatus, ValueType


@pytest.mark.parametrize('kind', ['commercial', 'eligibility', 'informational', 'unknown'])
def test_nontechnical_and_untyped_clauses_do_not_get_technical_alias_flags(kind):
    """Synthetic administrative clauses carry no technical attribute to map."""
    from app.schemas.enums import RequirementType
    from app.services.tender.ingest import RequirementRecord
    from app.services.requirement.structured_values import normalize_records
    row = RequirementRecord(text='Synthetic source clause', item_id='',
        requirement_type=RequirementType(kind), value='Synthetic source value',
        normalization={'source_context': {'source_role': 'cover', 'context_only': True}})
    normalize_records([row])
    assert not row.review_required and row.review_reason is None
    assert not row.normalization['review_required']
    assert row.normalization['source_context']['context_only']
    assert (row.text, row.value) == ('Synthetic source clause', 'Synthetic source value')


def test_normalizing_context_preserves_independent_review_and_unmapped_technical_flags():
    from app.schemas.enums import RequirementType
    from app.services.tender.ingest import RequirementRecord
    from app.services.requirement.structured_values import normalize_records
    context = RequirementRecord(text='Synthetic ambiguous scope', item_id='',
        requirement_type=RequirementType.UNKNOWN, review_required=True,
        review_reason='Synthetic scope needs review')
    technical = RequirementRecord(text='Synthetic property', item_id='1',
        attribute='Synthetic unmapped property', value='Synthetic value')
    normalize_records([context, technical])
    assert context.review_required and context.review_reason == 'Synthetic scope needs review'
    assert technical.review_required and technical.normalization['attribute'] is None
    assert technical.review_reason == 'unmapped attribute'


@pytest.mark.parametrize('value,unit,expected', [('2"', 'inch', '2'), ("6''", None, '6'),
    ('DN150', None, '6'), ('150', 'mm', '6'), ('NPS6', None, '6'),
    ('10–16 inches', None, '10-16')])
def test_nominal_source_labels(value, unit, expected):
    raw = (value, unit)
    result = normalize_fields('nominal_size', value, unit)
    assert not result['review_required'], result
    assert (result['value'], result['unit']) == (expected, 'inch')
    assert (value, unit) == raw


@pytest.mark.parametrize('value,unit', [('151', 'mm'), ('6', None), ('DN150', 'inch'),
    ('unknown150', 'mm'), ('16-10', 'inch'), ('6/0', 'inch')])
def test_unknown_conflicting_or_invalid_sizes_require_review(value, unit):
    assert normalize_fields('nominal_size', value, unit)['review_required']


def test_open_vocabulary_and_ambiguous_aliases():
    assert resolve_name('synthetic unseen property') == (None, 'unmapped attribute')
    aliases = [dict(alias='synthetic property', canonical_term=term, approved=True, confidence=1)
               for term in ('nominal_size', 'operating_pressure')]
    assert resolve_name('synthetic property', aliases) == (None, 'ambiguous attribute mapping')
    assert normalize_fields('material_grade', 'A216 WCB', None)['value'] == 'A216 WCB'


def test_nominal_tolerance_is_configuration(monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv('NOMINAL_SIZE_TOLERANCE', '0.01')
    get_settings.cache_clear()
    try:
        assert not normalize_fields('nominal_size', 'DN150.005', None)['review_required']
        assert normalize_fields('nominal_size', 'DN150.02', None)['review_required']
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize('offered,unit,status', [('6', 'inch', RequirementStatus.FULL_MATCH),
    ('5', 'inch', RequirementStatus.MISSING_CAPABILITY),
    ('151', 'mm', RequirementStatus.NO_EVIDENCE_FOUND),
    ('4-8', 'inch', RequirementStatus.FULL_MATCH)])
def test_comparison_uses_nominal_table(offered, unit, status):
    request = ComparisonRequest('150', Operator.EQ, 'mm', None, None,
                                offered, unit, None, None, attribute='nominal_size')
    assert evaluate(request, ValueType.NUMERIC).status is status


def test_ordinary_length_does_not_use_nominal_table():
    result = normalize_fields('face_to_face_distance', '150', 'mm')
    # Unapproved/unmapped attributes stay untouched, never inferred from units.
    assert result['value'] == '150'


def test_negative_mixed_fraction_keeps_sign_for_whole_value():
    result = normalize_fields('operating_pressure', '-1 1/2', 'bar')
    assert not result['review_required']
    assert result['value'] == '-1.5'


def test_alias_threshold_changes_normalization_lineage(monkeypatch):
    from app.core.config import get_settings
    from app.services.requirement.structured_values import fingerprint
    before = fingerprint()
    monkeypatch.setenv('ALIAS_CONFIDENCE_THRESHOLD', '0.91')
    get_settings.cache_clear()
    try:
        assert fingerprint() != before
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize('source,expected', [('2" (50mm)','2'),('DN150 (6 inches)','6'),
    ('50 mm (NPS2)','2'),('2–3 inches (50–80 mm)','2-3'),('1/2 inch (DN15)','0.5')])
def test_dual_nominal_labels_agree_by_configured_size_table(source,expected):
    result=normalize_fields('nominal_size',source,None)
    assert not result['review_required'] and result['value']==expected and result['unit']=='inch'


@pytest.mark.parametrize('source', ['6 inches (100 mm)','6 inches (152.4 mm)',
    '2 (50mm)','2" (unknown50)','2–3 inches (DN50)','2" (50 mm) optional'])
def test_dual_nominal_unknown_conflicting_and_clipped_ranges_stay_review_required(source):
    result=normalize_fields('nominal_size',source,None)
    assert result['review_required'] and result['value']==source


def test_normalization_preserves_original_quote_and_human_binding_evidence():
    from app.services.tender.ingest import RequirementRecord
    from app.services.requirement.structured_values import normalize_records
    source='Synthetic nominal size 2" (50mm)'
    review={'reviewer':'synthetic-reviewer','item_id':'synthetic-item'}
    row=RequirementRecord(text=source,source_text=source,attribute='nominal_size',value='2" (50mm)',
        item_id='synthetic-item',source_span=(23,len(source)),normalization={'human_review':review})
    before=(row.text,row.source_text,row.value,row.source_span)
    normalize_records([row])
    assert row.normalization['value']=='2' and row.normalization['human_review']==review
    assert (row.text,row.source_text,row.value,row.source_span)==before


@pytest.mark.parametrize('value,unit,expected', [
    ('25 to 200', 'DN', [1, 8]), ('2 - 24', 'NPS', [2, 24]),
    ('10" - 16"', None, [10, 16]), ('10”–16”', None, [10, 16]),
    ('10″–16″', None, [10, 16]), ('DN50 to DN80', None, [2, 3]),
    ('50 mm–80 mm', None, [2, 3]), ('1¼" to 2"', None, [1.25, 2]),
    ('1¼ to 2', 'NPS', [1.25, 2]),
])
def test_repeated_range_designators_and_fraction_glyphs_preserve_both_bounds(value, unit, expected):
    result = normalize_fields('nominal_size', value, unit)
    assert not result['review_required'], result
    assert result['nominal_nps'] == expected and result['range'] == expected
    assert result['unit'] == 'inch'


@pytest.mark.parametrize('value,unit', [
    ('10 inch–16 mm', None), ('DN50 to NPS3', None), ('2 to 3', 'unknown'),
    ('50 to 80', 'DNonsense'), ('DN50 to DN151', None), ('10"–16"', 'mm'),
])
def test_conflicting_or_unknown_range_labels_do_not_gain_a_normalized_value(value, unit):
    result = normalize_fields('nominal_size', value, unit)
    assert result['review_required'] and result['value'] == value and result['unit'] == unit


def test_nominal_designators_are_never_general_length_units():
    result = normalize_fields('operating_pressure', '50', 'DN')
    assert result['review_required'] and result['value'] == '50'


def test_non_nominal_si_unit_case_is_preserved():
    result = normalize_fields('operating_pressure', '150', 'kPa')
    assert not result['review_required'] and result['unit'] == 'kPa'
