"""Source-span support checks; no alias inference or offset repair."""
import re
import unicodedata
from decimal import Decimal

_NUMBER = r'[+-]?\d+(?:\.\d+)?'
_RANGE = re.compile(
    rf'(?<![\w.])({_NUMBER})\s*["”″]?\s*(?:[-–—]|\bto\b)\s*'
    rf'({_NUMBER})(?![\d.])', re.IGNORECASE)
_BETWEEN = re.compile(rf'\bbetween\s+({_NUMBER})\s+and\s+({_NUMBER})(?![\d.])', re.IGNORECASE)


def _normalise(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def _ranges(text):
    yield from _RANGE.finditer(text)
    yield from _BETWEEN.finditer(text)


def quote_span(text, quote):
    """Locate one verbatim quote, including overlapping duplicate detection."""
    if not isinstance(quote, str) or not quote.strip():
        return None
    start = text.find(quote)
    if start < 0 or text.find(quote, start + 1) >= 0:
        return None
    return start, start + len(quote)


def labelled_field_supported(text, span, attribute, value, table):
    """Bind a quoted value to exactly one literal source header and its cell."""
    if span is None or not attribute or not value or not table or not span_supports(text, span, value):
        return False
    if quote_span(text, text[span[0]:span[1]]) != tuple(span):
        return False
    key = lambda label: ' '.join(str(label).casefold().split())
    matches = [i for i, header in enumerate(table.get('headers', [])) if key(header) == key(attribute)]
    cells = table.get('cells', [])
    if len(matches) != 1 or matches[0] >= len(cells):
        return False
    cell = str(cells[matches[0]] or '')
    return span_supports(cell, (0, len(cell)), value)


def span_supports(text, span, value=None):
    """Require complete value support inside valid offsets, preserving ranges."""
    start, end = span
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        return False
    source = text[start:end]
    if not source.strip():
        return False
    if value is None:
        return True
    value = _normalise(value)
    if not value:
        return False
    # A clipped token cannot support the full value even if its substring agrees.
    if ((start and text[start - 1].isalnum() and text[start].isalnum())
            or (end < len(text) and text[end - 1].isalnum() and text[end].isalnum())):
        return False
    proposed_range = _RANGE.fullmatch(value) or _BETWEEN.fullmatch(value)
    if proposed_range:
        limits = tuple(Decimal(n) for n in proposed_range.groups())
        return limits[0] <= limits[1] and any(
            start <= match.start() and match.end() <= end
            and tuple(Decimal(n) for n in match.groups()) == limits for match in _ranges(text))
    if re.fullmatch(_NUMBER, value):
        number = Decimal(value)
        # Even a span clipped to one endpoint must not turn a range into a scalar.
        if any(start < match.end() and end > match.start()
               and number in tuple(Decimal(n) for n in match.groups())
               and Decimal(match[1]) != Decimal(match[2]) for match in _ranges(text)):
            return False
        return any(start <= match.start() and match.end() <= end and Decimal(match[0]) == number
                   for match in re.finditer(rf'(?<![\w.+-]){_NUMBER}(?![\d.])', text))
    # Formatting/case can differ; semantic aliases must remain reviewable.
    return re.search(r'(?<!\w)' + re.escape(value) + r'(?!\w)', _normalise(source)) is not None
