"""Heading/column source-region proposals; never a verified applicability claim."""
from collections import Counter
import math
import re
from app.services.textnorm import normalise_text

POLICY_VERSION = 'heading-regions-review-2'


def _normalise(text):
    return normalise_text(text or '').casefold()


def _valid(line):
    if not isinstance(line, dict) or not isinstance(line.get('text'), str):
        return False
    box, size = line.get('bbox'), line.get('font_size')
    return (isinstance(line.get('line_id'), str) and bool(line['line_id'])
            and isinstance(box, (list, tuple)) and len(box) == 4
            and all(type(n) in (int, float) and math.isfinite(n) for n in box)
            and box[0] < box[2] and box[1] < box[3]
            and type(size) in (int, float) and math.isfinite(size) and size > 0)


def _gutter(left, right, lines, top, bottom):
    gaps = [(left, right)] if left < right else []
    for line in lines:
        x0, y0, x1, y1 = line['bbox']
        if y0 < top or y1 > bottom:
            continue
        gaps = [(a, b) for low, high in gaps
                for a, b in ((low, min(high, x0)), (max(low, x1), high)) if a < b]
    return sum(gaps[0]) / 2 if len(gaps) == 1 else None


def heading_regions(lines):
    """Propose columns only from disjoint, aligned, overlapping heading bands."""
    if not isinstance(lines, list) or not lines or not all(_valid(line) for line in lines):
        return []
    if len({line['line_id'] for line in lines}) != len(lines):
        return []
    sizes = Counter()
    for line in lines:
        sizes[line['font_size']] += len(_normalise(line['text']))
    if not sizes or not max(sizes.values()):
        return []
    dominant = [size for size, weight in sizes.items() if weight == max(sizes.values())]
    if len(dominant) != 1:
        return []
    body_size = dominant[0]
    groups = []
    # Overlapping native heading rectangles may join a model and its caption.
    for line in sorted(lines, key=lambda item: (item['bbox'][1], item['bbox'][0])):
        if not line['text'].strip() or line['font_size'] <= body_size:
            continue
        box = line['bbox']
        overlaps = [g for g in groups if box[0] < g['bbox'][2] and g['bbox'][0] < box[2]
                    and box[1] < g['bbox'][3] and g['bbox'][1] < box[3]]
        joined = [line] + [old for group in overlaps for old in group['lines']]
        groups = [g for g in groups if g not in overlaps]
        groups.append({'lines': joined, 'bbox': [min(l['bbox'][0] for l in joined),
            min(l['bbox'][1] for l in joined), max(l['bbox'][2] for l in joined),
            max(l['bbox'][3] for l in joined)]})
    regions = []
    for group in groups:
        box = group['bbox']
        if len({l['bbox'][0] for l in group['lines']}) != 1:
            continue
        peers = sorted([g for g in groups if g['bbox'][1] < box[3] and box[1] < g['bbox'][3]],
                       key=lambda g: g['bbox'][0])
        if len(peers) < 2 or any(a['bbox'][2] >= b['bbox'][0] for a, b in zip(peers, peers[1:])):
            continue
        index = peers.index(group)
        left = ((peers[index - 1]['bbox'][2] + box[0]) / 2 if index
                else min(l['bbox'][0] for l in lines))
        right = ((box[2] + peers[index + 1]['bbox'][0]) / 2 if index + 1 < len(peers)
                 else max(l['bbox'][2] for l in lines))
        following = [g['bbox'][1] for g in groups if g['bbox'][1] >= box[3]
                     and g['bbox'][0] < right and left < g['bbox'][2]]
        bottom = min(following) if following else max(l['bbox'][3] for l in lines)
        if index:
            left = _gutter(peers[index - 1]['bbox'][2], box[0], lines, box[3], bottom)
        if index + 1 < len(peers):
            right = _gutter(box[2], peers[index + 1]['bbox'][0], lines, box[3], bottom)
        if left is None or right is None:
            continue
        members = [l for l in lines if left <= l['bbox'][0] and l['bbox'][2] <= right
                   and box[3] <= l['bbox'][1] and l['bbox'][3] <= bottom]
        # A line crossing a candidate gutter makes this region unresolved.
        if any(l['bbox'][1] >= box[3] and l['bbox'][3] <= bottom
               and l['bbox'][0] < right and left < l['bbox'][2]
               and not (left <= l['bbox'][0] and l['bbox'][2] <= right) for l in lines):
            continue
        regions.append({'heading_lines': group['lines'], 'bbox': [left, box[3], right, bottom],
                        'lines': members, 'method': POLICY_VERSION, 'review_required': True})
    return regions


def propose_region_anchor(lines, model, labels, value, quote, unit=None, conditions=None):
    """Propose one scoped attribute/value anchor; all results still need review."""
    model, value, quote = map(_normalise, (model, value, quote))
    if not model or not value or not quote:
        return None
    labels = {_normalise(label) for label in labels}
    expected = {value, _normalise(value + ' ' + (unit or ''))}
    matches = []
    regions = heading_regions(lines)
    if not regions or sum(_normalise(line['text']) == model for line in lines) != 1:
        return None
    for region in regions:
        if not any(_normalise(l['text']) == model for l in region['heading_lines']):
            continue
        for line in region['lines']:
            text = _normalise(line['text']).lstrip('•*- ')
            pair = re.split(r'[:=]', text, maxsplit=1)
            labelled = len(pair) == 2 and pair[0].strip() in labels and pair[1].strip() in expected
            explicit_value = value in labels and text in expected
            if quote not in text or not (labelled or explicit_value):
                continue
            if conditions and _normalise(conditions) not in text:
                continue
            matches.append({'model': model, 'heading_line_ids': [l['line_id'] for l in region['heading_lines']],
                'line_id': line['line_id'], 'bbox': line['bbox'], 'region_bbox': region['bbox'],
                'text': line['text'], 'method': POLICY_VERSION, 'review_required': True})
    return matches[0] if len(matches) == 1 else None
