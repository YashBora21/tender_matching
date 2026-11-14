"""Synthetic open-vocabulary field evidence; no API calls or vendor facts."""
import pytest
from app.services.tender.ingest import RequirementRecord
from app.services.requirement.coverage import uncovered_tokens
from app.services.requirement.evidence import labelled_field_supported
from app.services.requirement.retype import apply_clause_type
from app.services.requirement.structured_values import normalize_fields
from app.schemas.enums import RequirementType


def field(label='Synthetic Zeta',value='8 bar',*,section='Synthetic assembly',role='specification'):
    text=label+' '+value
    return RequirementRecord(text=text,source_text=text,item_id='synthetic',source_role=role,
        source_section=section,attribute=label,value=value,source_span=(len(label)+1,len(text)),
        table_context={'headers':[label],'cells':[value]},requirement_type=RequirementType.UNKNOWN)


@pytest.mark.parametrize('section',['Synthetic assembly','Technical specification'])
def test_verified_literal_label_is_read_without_approving_its_alias(section):
    r=field(section=section)
    assert uncovered_tokens(r.source_text,[r])==[]
    assert normalize_fields(r.attribute,r.value,None)['review_required']
    assert apply_clause_type(r) and r.requirement_type is RequirementType.TECHNICAL
    assert r.attribute=='Synthetic Zeta' and r.value=='8 bar'


@pytest.mark.parametrize('damage',['wrong_column','invented_label','duplicate_header','bad_quote','range_endpoint','duplicate_quote'])
def test_unsupported_labels_and_values_cannot_hide_source_tokens(damage):
    r=field()
    if damage=='wrong_column':r.table_context['cells']=['16 bar']
    if damage=='invented_label':r.attribute='Synthetic invented Zeta'
    if damage=='duplicate_header':r.table_context={'headers':[r.attribute,r.attribute],'cells':[r.value,r.value]}
    if damage=='bad_quote':r.source_span=None
    if damage=='range_endpoint':
        r=field(value='10–16 inches');r.value='10'
    if damage=='duplicate_quote':
        r.source_text+=' and 8 bar'
    assert not labelled_field_supported(r.source_text,r.source_span,r.attribute,r.value,r.table_context)
    assert 'synthetic' in uncovered_tokens(r.source_text,[r])


def test_real_unread_condition_remains_uncovered_and_explicit_atc_position_wins():
    r=field();r.source_text+=' optional ALPHA'
    assert 'alpha' in uncovered_tokens(r.source_text,[r])
    r=field(section='ATC');apply_clause_type(r)
    assert r.requirement_type is RequirementType.UNKNOWN
    r=field(role='supporting');apply_clause_type(r)
    assert r.requirement_type is RequirementType.UNKNOWN


def test_structural_quantity_does_not_become_a_product_property():
    r=field(label='Qty',value='8');apply_clause_type(r)
    assert r.requirement_type is RequirementType.UNKNOWN


def test_explicit_source_identifier_is_metadata_only_after_complete_verified_row_reading():
    r=field();r.source_text='1 '+r.source_text;r.source_span=tuple(n+2 for n in r.source_span)
    assert uncovered_tokens(r.source_text,[r])==['1']
    assert uncovered_tokens(r.source_text,[r],source_number='1')==[]
    assert uncovered_tokens(r.source_text,[r],source_number='2')==['1']
    r.source_text+=' optional ALPHA'
    assert {'1','alpha'}<=set(uncovered_tokens(r.source_text,[r],source_number='1'))
