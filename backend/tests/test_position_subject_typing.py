"""Synthetic source positions and subjects; no catalogue claims or model calls."""
import copy
import pytest
from app.schemas.enums import RequirementType
from app.services.tender import classify
from app.services.requirement.retype import apply_clause_type, retype
from app.services.tender.ingest import RequirementRecord


def record(text, section, *, kind=RequirementType.UNKNOWN, tag=None, attribute=None):
    return RequirementRecord(text=text, item_id='synthetic-item', page=1,
        source_section=section, page_tag=tag, requirement_type=kind, attribute=attribute)


def test_working_pressure_in_specification_is_technical():
    clause = record('Working pressure 8 bar', 'Technical specification')
    assert retype(clause)
    assert clause.requirement_type is RequirementType.TECHNICAL
    assert clause.value == '8' and clause.unit == 'bar'


def test_bidder_supply_history_in_atc_is_eligibility_despite_pressure_value():
    text = 'bidder supplied valves with pressure rating of 16 bar minimum'
    clause = record(text, 'ATC', kind=RequirementType.TECHNICAL)
    assert apply_clause_type(clause)
    assert clause.requirement_type is RequirementType.ELIGIBILITY
    assert clause.text == text


@pytest.mark.parametrize('section', ['ATC', 'Buyer added terms and conditions'])
def test_product_obligation_in_atc_is_technical_not_blanket_skipped(section):
    clause = record('The offered equipment shall have working pressure 8 bar', section,
                    tag='commercial_legal')
    assert retype(clause)
    assert clause.requirement_type is RequirementType.TECHNICAL


def test_narrow_atc_section_takes_precedence_over_coarse_technical_page_tag():
    decision = classify.classify_clause(
        'bidder supplied valves with pressure rating of 16 bar minimum',
        page_tag='technical', source_section='ATC')
    assert decision.kind == 'eligibility' and decision.source_position == 'atc'


@pytest.mark.parametrize('text,section', [
    ('It shall operate at pressure 8 bar', 'Technical specification'),
    ('Working pressure 8 bar', None),
    ('bidder supplied valves with pressure rating of 16 bar minimum', 'Technical specification'),
    ('Equipment shall have pressure 8 bar and bidder supplied similar equipment', 'ATC'),
])
def test_unresolved_subject_or_position_requires_review_without_a_guess(text, section):
    clause = record(text, section)
    assert not apply_clause_type(clause)
    assert clause.requirement_type is RequirementType.UNKNOWN
    assert clause.review_required and 'clause_type_review' in clause.review_reason


def test_specification_rating_is_not_a_certificate_because_model_called_it_one():
    clause = record('ASME class 600', 'Technical specification',
        kind=RequirementType.CERTIFICATION, attribute='pressure_class')
    assert apply_clause_type(clause)
    assert clause.requirement_type is RequirementType.TECHNICAL


def test_source_not_display_paraphrase_determines_subject():
    clause = record('Working pressure 16 bar', 'ATC', kind=RequirementType.TECHNICAL)
    clause.source_text = 'bidder supplied valves with pressure rating of 16 bar minimum'
    assert apply_clause_type(clause)
    assert clause.requirement_type is RequirementType.ELIGIBILITY


def test_custom_property_is_data_driven_and_not_a_category_word(monkeypatch):
    policy = copy.deepcopy(classify.clause_policy())
    policy['retype']['attribute_words']['synthetic coupling geometry'] = 'synthetic_coupling_geometry'
    monkeypatch.setattr(classify, 'clause_policy', lambda: policy)
    decision = classify.classify_clause('Synthetic coupling geometry: X7', source_section='specification')
    assert decision.kind == 'technical'


def test_policy_fingerprint_changes_with_semantic_data(monkeypatch):
    before = classify.classification_policy_fingerprint()
    policy = copy.deepcopy(classify.clause_policy())
    policy['version'] = 'synthetic-policy-change'
    monkeypatch.setattr(classify, 'clause_policy', lambda: policy)
    assert classify.classification_policy_fingerprint() != before


def test_existing_review_reason_is_preserved_when_context_remains_ambiguous():
    clause = record('Working pressure 8 bar', None)
    clause.review_required = True
    clause.review_reason = 'synthetic source identity unresolved'
    apply_clause_type(clause)
    assert 'synthetic source identity unresolved' in clause.review_reason


def test_pipeline_decisive_eligibility_avoids_extractor(monkeypatch):
    from app.services.tender import pipeline
    clause = record('bidder supplied valves with pressure rating of 16 bar minimum', 'ATC')
    monkeypatch.setattr(pipeline, '_extract_one', lambda *a, **k: pytest.fail('eligibility sent to extractor'))
    rows, calls, hits, limited, error = pipeline._extract_all([clause])
    assert rows[0].requirement_type is RequirementType.ELIGIBILITY
    assert calls == 0 and hits == 0 and not limited and error is None


def test_pipeline_product_atc_retains_source_context(monkeypatch):
    from app.services.tender import pipeline
    clause = record('Working pressure 8 bar', 'ATC', tag='commercial_legal')
    def extracted(*a, **k):
        return [record('Working pressure 8 bar', None, kind=RequirementType.CERTIFICATION,
                       attribute='operating_pressure')], True, False, None
    monkeypatch.setattr(pipeline, '_extract_one', extracted)
    rows, calls, *_ = pipeline._extract_all([clause])
    assert calls == 1
    assert rows[0].requirement_type is RequirementType.TECHNICAL
    assert rows[0].page_tag == clause.page_tag
    assert rows[0].source_section == 'ATC'
    assert rows[0].source_text == clause.text


def test_pipeline_cache_roundtrip_preserves_context_and_normalization():
    from app.services.tender import pipeline
    clause = record('Working pressure 8 bar', 'Specification', tag='technical')
    clause.normalization = {'attribute': 'operating_pressure', 'value': '8', 'unit': 'bar'}
    recovered = pipeline._as_record(pipeline._as_dict(clause))
    assert recovered.normalization == clause.normalization
    assert recovered.page_tag == clause.page_tag
    assert recovered.source_section == clause.source_section
    stored = pipeline.to_stored(clause, 'synthetic-tender')
    assert stored.normalization['source_context']['page_tag'] == 'technical'


def test_pipeline_normalization_policy_only_invalidates_resolution(monkeypatch):
    from app.services.tender import pipeline
    kwargs = dict(prompt_version='synthetic', schema_version='synthetic', model='synthetic', vocabulary_hash='synthetic')
    before = pipeline.stage_keys([], **kwargs)
    monkeypatch.setattr(pipeline, 'normalization_fingerprint', lambda: 'synthetic-policy-change')
    after = pipeline.stage_keys([], **kwargs)
    assert [key for key in before if before[key] != after[key]] == [pipeline.STAGE_RESOLUTION]
