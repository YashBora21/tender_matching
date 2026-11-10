"""Geometric OCR rows. Retain uncertain cells; never infer missing text."""
from __future__ import annotations

from statistics import median


def lines_from_boxes(boxes, overlap):
    slopes = []
    for box in boxes:
        p = box['polygon']
        dx, dy = p[1][0] - p[0][0], p[1][1] - p[0][1]
        # Estimate the common text baseline; vertical stamps cannot define a row.
        if dx and abs(dy) < abs(dx):
            slopes.append(dy / dx)
    slope = median(slopes) if slopes else 0.0
    words = []
    for index, box in enumerate(boxes):
        p = box['polygon']
        ys = [y - slope * x for x, y in p]
        words.append({'index': index, 'x': sum(x for x, _ in p) / len(p),
                      'low': min(ys), 'high': max(ys)})
    groups = []
    for word in sorted(words, key=lambda w: (w['low'], w['x'])):
        matches = [group for group in groups if
                   min(word['high'], group['high']) - max(word['low'], group['low']) >=
                   overlap * min(word['high'] - word['low'], group['high'] - group['low'])]
        if len(matches) == 1:
            group = matches[0]
            group['words'].append(word)
            # An intersection avoids a tall box transitively merging adjacent rows.
            group['low'] = max(group['low'], word['low'])
            group['high'] = min(group['high'], word['high'])
        else:
            groups.append({'low': word['low'], 'high': word['high'], 'words': [word]})
    return [sorted(group['words'], key=lambda w: w['x']) for group in
            sorted(groups, key=lambda g: g['low'])]


def page_layout(reading, settings, header_predicate=None):
    boxes = reading['boxes']
    groups = lines_from_boxes(boxes, settings.ocr_line_overlap)
    text = '\n'.join(' | '.join(boxes[w['index']]['text'] for w in group) for group in groups)
    header_index = next((i for i, group in enumerate(groups)
                         if len(group) >= settings.ocr_table_min_columns and
                         (header_predicate is None or header_predicate([boxes[w['index']]['text'] for w in group]))), None)
    if header_index is None:
        return {'text': text, 'headers': [], 'rows': [], 'reading': reading}
    header = list(groups[header_index])
    labels = {word['index']: boxes[word['index']]['text'] for word in header}
    start = header_index + 1
    while start < len(groups) and len(groups[start]) < settings.ocr_table_min_columns:
        for word in groups[start]:
            polygon = boxes[word['index']]['polygon']
            left, right = min(p[0] for p in polygon), max(p[0] for p in polygon)
            matches = [h for h in header if min(right, max(p[0] for p in boxes[h['index']]['polygon'])) >
                       max(left, min(p[0] for p in boxes[h['index']]['polygon']))]
            if len(matches) == 1:
                index = matches[0]['index']
                labels[index] += ' ' + boxes[word['index']]['text']
            else:
                header.append(word)
                labels[word['index']] = boxes[word['index']]['text']
        start += 1
    header.sort(key=lambda w: w['x'])
    centers = [word['x'] for word in header]
    headers = [labels[word['index']] for word in header]
    boundaries = [(left + right) / 2 for left, right in zip(centers, centers[1:])]
    columns = [[] for _ in header]
    for group in groups[start:]:
        for word in group:
            columns[sum(word['x'] > boundary for boundary in boundaries)].append(word)
    # An explicit source identifier column anchors row order without matching any business value.
    anchor = next((i for i, label in enumerate(headers)
                   if ''.join(c for c in label.casefold() if c.isalnum()) in
                   {'partno', 'partnumber', 'oempartnumber'}), None)
    if anchor is None or not columns[anchor]:
        return {'text': text, 'headers': headers, 'rows': [], 'reading': reading}
    anchors = sorted(columns[anchor], key=lambda word: word['low'])
    ys = [(word['low'] + word['high']) / 2 for word in anchors]
    spacing = median([b - a for a, b in zip(ys, ys[1:])]) if len(ys) > 1 else anchors[0]['high'] - anchors[0]['low']
    rows = [{'cells': [''] * len(headers), 'box_indices': [[] for _ in header],
             'ambiguous': False, 'row_id': str(i + 1)} for i in range(len(anchors))]
    for column, words in enumerate(columns):
        words = sorted([word for word in words if ys[0] - spacing / 2 <=
                        (word['low'] + word['high']) / 2 <= ys[-1] + spacing / 2], key=lambda word: word['low'])
        if len(words) == len(rows):
            assignments = list(range(len(rows)))
        else:
            assignments = [min(range(len(rows)), key=lambda i: abs(ys[i] - (word['low'] + word['high']) / 2))
                           for word in words]
        for word, index in zip(words, assignments):
            rows[index]['cells'][column] = (rows[index]['cells'][column] + ' ' + boxes[word['index']]['text']).strip()
            rows[index]['box_indices'][column].append(word['index'])
            if len(words) != len(rows):
                rows[index]['ambiguous'] = True
    return {'text': text, 'headers': headers, 'rows': rows, 'reading': reading}
