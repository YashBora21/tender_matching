"""Conservative native-line binding; never assign a column by proximity."""
import math
import re
from app.services.textnorm import normalise_text

POLICY_VERSION = 'native-inline-1-heading-proposals-2'


def bind_inline_model(lines, model, labels, value, quote, unit=None, conditions=None):
    """Resolve one explicit `model; attribute: value` native source line."""
    normalise = lambda text: normalise_text(text or '').casefold()
    model, value, quote = normalise(model), normalise(value), normalise(quote)
    if not model or not value or not quote or not isinstance(lines, list):
        return None
    labels = {normalise(label) for label in labels}
    expected = {value, normalise(value + ' ' + (unit or ''))}
    matches = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        if not isinstance(line.get('line_id'), str) or not line['line_id']:
            continue
        bbox = line.get('bbox', [])
        if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
                or any(type(n) not in (int, float) or not math.isfinite(n) for n in bbox)
                or not bbox[0] < bbox[2] or not bbox[1] < bbox[3]):
            continue
        text = normalise(line.get('text'))
        # shortcut: complex prose/heading layouts require review until explicit region bindings exist.
        fields = re.split(r'[;|]', text)
        if len(fields) != 2 or fields[0].strip() != model or quote not in text:
            continue
        pair = re.split(r'[:=]', fields[1], maxsplit=1)
        if len(pair) != 2 or pair[0].strip() not in labels or pair[1].strip() not in expected:
            continue
        if conditions and normalise(conditions) not in text:
            continue
        matches.append({'line_id': line.get('line_id'), 'bbox': list(bbox),
                        'model': model, 'text': line['text'], 'method': POLICY_VERSION})
    return matches[0] if len(matches) == 1 else None
