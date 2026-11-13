"""Deterministic source-role triage before item assignment or a model call."""
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re

from app.comparison.attributes import certification_pattern, clause_markers
from app.core.config import get_settings
from app.services.tender.classify import (
    classify_clause, clause_policy, classification_policy_fingerprint,
    reads_as_legal_clause, NUMBER_WITH_UNIT,
)


@lru_cache(maxsize=1)
def policy():
    return json.loads(Path(__file__).with_name('source_policy.json').read_text(encoding='utf-8'))


def fingerprint():
    from app.services.tender.templates import reviewed_data
    from app.comparison.vocabulary import vocabulary_path
    settings = get_settings()
    return sha256(json.dumps([policy(), classification_policy_fingerprint(),
                              certification_pattern().pattern, reviewed_data(),
                              settings.req_min_chars, settings.req_min_words,
                              Path(vocabulary_path()).read_text(encoding='utf-8')], sort_keys=True).encode()).hexdigest()


def source_role(source, *, specification=False):
    roles = policy()
    explicit = source.get('source_role') or source.get('role')
    if explicit in roles['primary_roles'] + roles['restricted_roles']:
        return explicit
    if source.get('source_key') == 'wrapper':
        return 'cover'
    for role, patterns in roles['restricted_filename_patterns'].items():
        if any(re.search(pattern, Path(str(source.get('path') or '')).name, re.I) for pattern in patterns):
            return role
    if source.get('kind') in {'boq-csv', 'boq-pdf'}:
        return 'boq'
    if source.get('kind') == 'spec' or specification:
        return 'specification'
    return 'supporting'


@dataclass(frozen=True)
class SourceDecision:
    source_role: str
    context_only: bool
    candidate_type: str | None
    clause_type: str
    reason: str
    fragment_reason: str | None = None


def triage(text, role, *, page_tag=None, section=None, table=False, aliases=()):
    """A candidate permits reading; it never establishes a capability match."""
    if certification_pattern().search(text):
        return SourceDecision(role, False, 'certification', 'certification',
                              'Source cites an identifiable certification or standard')
    settings = get_settings()
    prose = ' '.join(text.split())
    short = (role in policy()['fragment_prose_roles'] and not table and
             (len(prose) < settings.req_min_chars or len(prose.split()) < settings.req_min_words))
    if short:
        from app.services.requirement.structured_values import resolve_name
        canonical, alias_error = resolve_name(prose, aliases)
        if canonical or alias_error in {'ambiguous attribute mapping', 'unapproved attribute mapping'}:
            return SourceDecision(role, False, 'technical', 'technical' if canonical else 'unknown',
                                  'Approved attribute label' if canonical else alias_error)
    decision = classify_clause(text, page_tag=page_tag, source_section=section)
    if decision.kind in policy()['nontechnical_types'] and not decision.review_required:
        return SourceDecision(role, True, None, decision.kind, decision.reason)
    cues = clause_policy()
    properties = [word for word in cues['retype']['attribute_words']
                  if re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)', text, re.I)]
    actor = re.search(cues['competing_actor_pattern'], text, re.I)
    product_obligation = re.search(cues['product_obligation_pattern'], text, re.I)
    measured_property = bool(properties and NUMBER_WITH_UNIT.search(text))
    product_subject = bool((properties or product_obligation) and not actor and not reads_as_legal_clause(text))
    product_clause = decision.kind == 'technical' or measured_property or product_subject
    if product_clause:
        return SourceDecision(role, False, 'technical', 'technical',
                              'Source subject may state a product property')
    if reads_as_legal_clause(text):
        return SourceDecision(role, True, None, 'commercial',
                              'Source states an administrative or legal obligation')
    from app.services.tender.templates import reviewed_template
    template = reviewed_template(text, role)
    if template:
        kind = template['type']
        context = kind in policy()['nontechnical_types']
        return SourceDecision(role, context, None if context else kind, kind,
                              'Reviewed template: ' + template['template_id'])
    if short:
        markers = clause_markers()
        obligation = markers and re.search(r'\b(?:' + '|'.join(re.escape(word) for word in markers) + r')\b', prose, re.I)
        inherited = decision.source_position == 'specification' or (section and (
            classify_clause(section, page_tag=page_tag).kind in {'technical', 'certification'} or
            resolve_name(section, aliases)[0] is not None))
        if NUMBER_WITH_UNIT.search(prose) or obligation or inherited or page_tag == 'technical':
            return SourceDecision(role, False, 'technical', 'unknown',
                                  'Short prose retains a quantity, obligation or technical source context')
        reason = ('fragment (below prose floor, no signal): '
                  f'{len(prose)} chars/{len(prose.split())} words; '
                  f'floors {settings.req_min_chars} chars/{settings.req_min_words} words')
        return SourceDecision(role, True, None, 'unknown', reason, reason)
    if role in policy()['primary_roles']:
        return SourceDecision(role, False, 'technical', 'unknown',
                              'Primary source line needs interpretation')
    return SourceDecision(role, True, None, 'unknown',
                          'Restricted-source context has no deterministic technical/certification signal')


def source_context(row):
    context = dict((row.get('normalization') or {}).get('source_context') or {})
    if row.get('source_role') is not None:
        context.update(source_role=row['source_role'], context_only=bool(row.get('context_only')),
                       candidate_type=row.get('candidate_type'))
    if row.get('fragment_reason'):
        context['fragment_reason'] = row['fragment_reason']
    return context


def context_only(row):
    return source_context(row).get('context_only') is True


def blocking_requirement(row):
    if context_only(row):
        return False
    kind = row.get('type', row.get('requirement_type'))
    if kind in policy()['nontechnical_types']:
        return False
    context = source_context(row)
    if context.get('source_role') in policy()['restricted_roles']:
        return context.get('candidate_type') in {'technical', 'certification'}
    # Legacy/primary readings remain conservative until their sources are re-triaged.
    return True
