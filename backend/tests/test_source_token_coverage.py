"""Synthetic source content across domains; this is a review trigger, not semantic proof."""
from types import SimpleNamespace
from app.services.requirement.coverage import uncovered_tokens


def fact(attribute,value,unit=None):
    return SimpleNamespace(attribute=attribute,value=value,unit=unit,operator='eq',
                           text='synthetic paraphrase',source_span=(0,100))


def test_attributes_and_values_cover_source_content_without_using_paraphrase():
    assert uncovered_tokens('pressure 12 bar and temperature 40 C',
        [fact('pressure','12','bar'),fact('temperature','40','C')])==[]


def test_one_numeric_fact_cannot_cover_an_extra_qualitative_facet():
    source='size 25 mm mounting arrangement ZETA'
    value=fact('size','25','mm');value.text=source
    assert 'zeta' in uncovered_tokens(source,[value])


def test_unapproved_attribute_cannot_launder_source_words():
    source='size 25 mm mounting arrangement ZETA'
    assert 'mounting' in uncovered_tokens(source,[fact('synthetic mounting arrangement size',source)])


def test_empty_facts_leave_source_content_uncovered():
    assert uncovered_tokens('pressure 12 bar',[])==['12','bar','pressure']
