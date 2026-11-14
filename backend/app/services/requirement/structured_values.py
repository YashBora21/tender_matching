"""Typed derived fields kept beside immutable source values and quotes."""
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata

from app.comparison.attributes import attribute_by_term
from app.comparison.vocabulary import load_vocabulary, vocabulary_path, normalise_token
from app.comparison.units import parse_unit
from app.core.config import get_settings
from app.schemas.enums import RequirementType

VERSION = 'structured-values-4'
_NUMBER = r'[+-]?(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)'
_QUANTITY = re.compile(rf'^\s*(?P<prefix>[A-Za-z]+)?\s*(?P<low>{_NUMBER})(?:\s*(?P<first_unit>[^\d]*?)\s*(?:–|—|-|\bto\b)\s*(?P<high_prefix>[A-Za-z]+)?\s*(?P<high>{_NUMBER}))?\s*(?P<unit>[^\d]*)\s*$')


def policy_path():
    return Path(get_settings().normalization_policy_path or Path(__file__).resolve().parents[3] / 'data' / 'normalization_policy.json')


def fingerprint():
    return sha256((VERSION + policy_path().read_text(encoding='utf-8')
                   + Path(vocabulary_path()).read_text(encoding='utf-8')
                   + str(get_settings().nominal_size_tolerance)
                   + str(get_settings().alias_confidence_threshold)).encode()).hexdigest()


def resolve_name(name, aliases=(), *, threshold=None):
    """Exact data mappings only; normalization collisions stay ambiguous."""
    token = normalise_token(name or '')
    candidates = []
    overridden = {normalise_token(row['alias']) for row in aliases}
    for spec in load_vocabulary(vocabulary_path()).attributes:
        for term in (spec.canonical_term, *spec.synonyms):
            if token and normalise_token(term) == token and (term == spec.canonical_term or token not in overridden):
                candidates.append((spec.canonical_term, spec.approved and
                    (term == spec.canonical_term or term in spec.approved_synonyms)))
    for row in aliases:
        if token and normalise_token(row['alias']) == token:
            candidates.append((row['canonical_term'], bool(row['approved']) and
                               float(row.get('confidence') or 0) >= (get_settings().alias_confidence_threshold if threshold is None else threshold)))
    terms = {term for term, _ in candidates}
    if len(terms) > 1:
        return None, 'ambiguous attribute mapping'
    if not terms:
        return None, 'unmapped attribute'
    term = next(iter(terms))
    spec = attribute_by_term(term)
    if spec is None or not spec.approved or not any(approved for _, approved in candidates):
        return None, 'unapproved attribute mapping'
    return term, None


def _number(text):
    parts = text.split()
    magnitude = sum(abs(float(Fraction(part))) for part in parts)
    return -magnitude if text.startswith('-') else magnitude


def _quantity_text(value):
    # NFKC alone turns 1¼ into 11/4, changing the magnitude.
    spaced = ''.join((' ' if i and value[i-1].isdigit() and
                      unicodedata.decomposition(char).startswith('<fraction>') else '') + char
                     for i, char in enumerate(value or ''))
    return unicodedata.normalize('NFKC', spaced).replace('⁄', '/')


def normalize_fields(attribute, value, unit, *, aliases=()):
    """Normalize only an approved typed attribute; never change raw input fields."""
    canonical, reason = resolve_name(attribute, aliases)
    result = {'policy': fingerprint(), 'attribute': canonical, 'value': value, 'unit': unit,
              'review_required': bool(reason), 'review_reason': reason}
    if canonical is None:
        return result
    spec = attribute_by_term(canonical)
    if spec.value_type.value not in ('numeric', 'range'):
        return result
    policy = json.loads(policy_path().read_text(encoding='utf-8'))
    text = _quantity_text(value)
    if getattr(spec, 'normalization', None) == 'nominal':
        annotated = re.fullmatch(r'\s*([^()]+?)\s*\(([^()]+)\)\s*', text)
        if annotated:
            primary = normalize_fields(canonical, annotated[1].strip(), unit, aliases=aliases)
            secondary = normalize_fields(canonical, annotated[2].strip(), None, aliases=aliases)
            if primary['review_required'] or secondary['review_required']:
                return dict(result, review_required=True, review_reason='unresolved nominal size annotation')
            if primary.get('nominal_dn') != secondary.get('nominal_dn'):
                return dict(result, review_required=True, review_reason='conflicting nominal size annotations')
            return primary
    match = _QUANTITY.fullmatch(text)
    if not match:
        return dict(result, review_required=True, review_reason='unresolved structured numeric value')
    strategy = getattr(spec, 'normalization', None)
    systems = []
    units = []
    aliases_by_unit = {unicodedata.normalize('NFKC', key).casefold(): val
                       for key, val in policy['unit_aliases'].items()}
    for designator in (match['prefix'], match['high_prefix']):
        if designator:
            system = policy['nominal_prefixes'].get(designator.casefold()) if strategy == 'nominal' else None
            if system is None:
                return dict(result, review_required=True, review_reason='unknown numeric prefix')
            systems.append(system)
    for declared in (unit, match['first_unit'], match['unit']):
        if not declared or not declared.strip():
            continue
        spelling = unicodedata.normalize('NFKC', declared.strip())
        token = spelling.casefold()
        system = policy['nominal_prefixes'].get(token) if strategy == 'nominal' else None
        if system:
            systems.append(system)
            continue
        canonical_unit = aliases_by_unit.get(token) or parse_unit(spelling)
        if canonical_unit is None:
            return dict(result, review_required=True, review_reason='unknown numeric unit')
        units.append(canonical_unit)
        if strategy == 'nominal':
            system = policy['nominal_units'].get(canonical_unit)
            if system is None:
                return dict(result, review_required=True, review_reason='unknown nominal designator')
            systems.append(system)
    if len(set(units)) > 1 or len(set(systems)) > 1:
        return dict(result, review_required=True, review_reason='conflicting numeric units or nominal designators')
    canonical_unit = units[0] if units else None
    try:
        numbers = [_number(match['low'])] + ([_number(match['high'])] if match['high'] else [])
    except (ValueError, ZeroDivisionError):
        return dict(result, review_required=True, review_reason='invalid numeric value')
    if len(numbers) == 2 and numbers[0] > numbers[1]:
        return dict(result, review_required=True, review_reason='reversed numeric range')
    if strategy == 'nominal':
        system = systems[0] if systems else None
        if system is None:
            return dict(result, review_required=True, review_reason='nominal size requires an explicit DN/NPS/unit label')
        mapped = []
        for number in numbers:
            rows = [row for row in policy['nominal_sizes']
                    if abs(float(row[system]) - number) <= get_settings().nominal_size_tolerance]
            if len(rows) != 1:
                return dict(result, review_required=True, review_reason='unknown or ambiguous nominal size')
            mapped.append(rows[0])
        numbers = [row['nps'] for row in mapped]
        result['nominal_dn'] = [row['dn'] for row in mapped]
        result['nominal_nps'] = numbers
        result['kind'] = 'nominal'
        canonical_unit = 'inch'
    result.update(value='-'.join(f'{number:g}' for number in numbers), unit=canonical_unit,
                  range=numbers if len(numbers) == 2 else None)
    return result


def normalize_records(records, aliases=()):
    for record in records:
        metadata = {key: record.normalization[key] for key in ('source_context', 'human_review')
                    if key in (record.normalization or {})}
        if record.requirement_type not in {RequirementType.TECHNICAL, RequirementType.CERTIFICATION}:
            # Clause-type/scope uncertainty is independent of technical alias resolution.
            record.normalization = {'policy': fingerprint(), 'attribute': None,
                'value': record.value, 'unit': record.unit,
                'review_required': False, 'review_reason': None}
            record.normalization.update(metadata)
            continue
        record.normalization = normalize_fields(record.attribute, record.value, record.unit, aliases=aliases)
        record.normalization.update(metadata)
        if record.normalization['review_required']:
            record.review_required = True
            record.review_reason = '; '.join(filter(None, (record.review_reason, record.normalization['review_reason'])))
    return records
