"""Synthetic source values: normalization never edits source evidence."""
import pytest

from app.services.requirement.structured_values import normalize_fields, resolve_name
from app.comparison.registry import ComparisonRequest, evaluate
from app.schemas.enums import Operator, RequirementStatus, ValueType


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
