"""Synthetic columns, headings and labels; region results are proposals only."""
import copy
import pytest
from app.services.match.source_regions import heading_regions, propose_region_anchor


def native(text, x, y, width=180, size=10):
    return {'line_id': f'synthetic:{x}:{y}', 'text': text,
            'bbox': [x, y, x + width, y + size], 'font_size': size}


def source():
    return [native('Synthetic A', 20, 20, 80, 16), native('Synthetic B', 300, 20, 80, 16),
            native('Coupling geometry: X7', 20, 80), native('Coupling geometry: Y8', 300, 80),
            native('Synthetic background content retained without making a capability claim.', 20, 110),
            native('Synthetic unrelated background content retained for source review.', 300, 110)]


def anchor(lines, **changes):
    args = dict(model='Synthetic A', labels=['coupling geometry'], value='X7', quote='X7')
    args.update(changes)
    return propose_region_anchor(lines, **args)


def test_valid_column_anchor_retains_source_geometry_and_review_state():
    lines = source()
    result = anchor(lines)
    assert result and result['review_required']
    assert result['line_id'] == lines[2]['line_id'] and result['bbox'] == lines[2]['bbox']
    assert result['heading_line_ids'] == [lines[0]['line_id']]


def test_value_is_not_borrowed_from_another_models_column():
    assert anchor(source(), value='Y8', quote='Y8') is None
    assert anchor(source(), model='Synthetic B') is None
    assert anchor(source(), model='Synthetic A0') is None


def test_unknown_attribute_labels_are_not_guessed():
    assert anchor(source(), labels=['other geometry']) is None


def test_identical_quotes_in_two_columns_keep_distinct_model_anchors():
    lines = source()
    lines[3]['text'] = lines[2]['text']
    a, b = anchor(lines), anchor(lines, model='Synthetic B')
    assert a and b and a['line_id'] != b['line_id']


def test_repeated_quote_within_one_model_region_remains_ambiguous():
    lines = source() + [native('Coupling geometry: X7', 20, 95)]
    assert anchor(lines) is None


def test_later_competing_heading_stops_the_earlier_model_region():
    lines = source()
    lines[2]['text'] = 'Coupling geometry: Z1'
    lines += [native('Synthetic C', 20, 140, 80, 16), native('Coupling geometry: X7', 20, 180)]
    assert anchor(lines) is None


def test_wrapped_overlapping_caption_does_not_merge_adjacent_columns():
    lines = source() + [native('Synthetic caption A', 20, 34, 180, 16),
                        native('Synthetic caption B', 300, 34, 180, 16)]
    a, b = anchor(lines), anchor(lines, model='Synthetic B', value='Y8', quote='Y8')
    assert a and b and len(a['heading_line_ids']) == 2
    assert a['region_bbox'][2] < b['region_bbox'][0] or a['region_bbox'][2] == b['region_bbox'][0]


def test_text_crossing_the_candidate_gutter_blocks_region_proposals():
    assert anchor(source() + [native('Synthetic shared scope with unresolved applicability', 20, 95, 450)]) is None


def test_overlapping_model_headings_do_not_produce_a_region():
    lines = source()
    lines[0]['bbox'][2] = 350
    assert not heading_regions(lines)


def test_uniform_typography_is_not_inferred_as_a_heading():
    lines = source()
    for line in lines: line['font_size'] = 10
    assert not heading_regions(lines)


def test_conditions_cannot_be_borrowed_from_other_lines():
    assert anchor(source(), conditions='Synthetic upgrade required') is None


def test_value_only_line_requires_an_explicit_approved_attribute_label():
    lines = source()
    lines[2]['text'] = 'X7'
    assert anchor(lines) is None
    assert anchor(lines, labels=['coupling geometry', 'X7'])


@pytest.mark.parametrize('change', [{'font_size': 0}, {'bbox': [0, 0, float('nan'), 20]}, {'line_id': None}])
def test_malformed_geometry_is_not_used_to_infer_gutters(change):
    lines = copy.deepcopy(source())
    lines[2].update(change)
    assert not heading_regions(lines)
