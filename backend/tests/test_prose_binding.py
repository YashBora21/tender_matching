"""Synthetic native lines; no vendor-specific headings or inferred columns."""
import pytest
from app.services.match.prose_binding import bind_inline_model


def line(text, line_id='synthetic:1', bbox=None):
    return {'text': text, 'line_id': line_id, 'bbox': bbox if bbox is not None else [10, 10, 300, 25]}


def bind(lines, **changes):
    kwargs = dict(model='Synthetic Z9', labels=['coupling geometry'], value='X7', quote='X7')
    kwargs.update(changes)
    return bind_inline_model(lines, **kwargs)


def test_explicit_inline_model_attribute_and_value_retains_native_anchor():
    source = line('Synthetic Z9; Coupling geometry: X7')
    anchor = bind([source])
    assert anchor['line_id'] == source['line_id'] and anchor['bbox'] == source['bbox']
    assert source['text'] == anchor['text']


@pytest.mark.parametrize('text', [
    'Synthetic Y8; Coupling geometry: X7',
    'Synthetic Z9; Other geometry: X7',
    'Synthetic Z9; Coupling geometry: X70',
    'Synthetic Z90; Coupling geometry: X7',
    'Synthetic Z9 / Synthetic Y8; Coupling geometry: X7',
    'Synthetic Z9; Other geometry: X7; Coupling geometry: Y8',
    'Synthetic Z9; Synthetic Y8; Coupling geometry: X7',
    'Synthetic Z9; Coupling geometry: X7 optional',
])
def test_wrong_scope_attribute_value_or_ambiguous_line_is_not_bound(text):
    assert bind([line(text)]) is None


def test_page_wide_model_and_quote_do_not_bind_across_lines_or_columns():
    assert bind([line('Synthetic Z9'), line('Coupling geometry: X7', 'synthetic:2')]) is None


def test_repeated_anchors_remain_ambiguous():
    text = 'Synthetic Z9; Coupling geometry: X7'
    assert bind([line(text), line(text, 'synthetic:2')]) is None


def test_numeric_units_and_exact_conditions():
    source = line('Synthetic Z9 | Pressure = 12 bar')
    kwargs = dict(labels=['pressure'], value='12', quote='12 bar', unit='bar')
    assert bind([source], **kwargs)
    assert bind([source], **kwargs, conditions='Synthetic upgrade only') is None


@pytest.mark.parametrize('bbox', [[], [0, 0, 0, 10], [10, 10, 0, 0], [0, 0, float('nan'), 10]])
def test_invalid_native_coordinates_do_not_create_an_anchor(bbox):
    assert bind([line('Synthetic Z9; Coupling geometry: X7', bbox=bbox)]) is None


@pytest.mark.parametrize('lines', [None, {}, ['not a source line'], [dict(line('Synthetic Z9; Coupling geometry: X7'), bbox=5)]])
def test_malformed_stored_layout_remains_unbound(lines):
    assert bind(lines) is None
