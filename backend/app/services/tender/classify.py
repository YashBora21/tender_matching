"""Page triage: what kind of page is this, without asking a model (FR-T1).

A tender is not one document. The same PDF carries the buyer's specification,
the integrity pact, the safety annex and pages of letterhead. Sending every
page to the model is what floods the requirement list with legal prose and
safety instructions, and it costs a call per line on a provider with an
8,000-token-per-minute limit.

This module answers one question cheaply and deterministically: is this page
technical, commercial/legal, safety/HSE, boilerplate, or a mix? The answer
decides whether the page is read at all, and it is stored on the page ledger
so a reviewer can see why.

The source-position, subject and page-scoring cues are versioned JSON data.
"""

from __future__ import annotations

import re
import json
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from dataclasses import dataclass, field


@lru_cache(maxsize=1)
def clause_policy():
    """Versioned source-position/subject cues shipped as editable data."""
    # shortcut: release policy is loaded once per process; restart after editing its data.
    return json.loads(Path(__file__).with_name('clause_policy.json').read_text(encoding='utf-8'))


def classification_policy_fingerprint():
    return sha256(json.dumps(clause_policy(), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ClauseClassification:
    kind: str
    source_position: str
    reason: str
    review_required: bool = False


def _position(page_tag, source_section):
    for context in (source_section, page_tag):
        if not context:
            continue
        positions = [name for name, patterns in clause_policy()['position_patterns'].items()
                     if any(re.search(pattern, context, re.IGNORECASE) for pattern in patterns)]
        if len(positions) == 1:
            return positions[0]
        if positions:
            return 'ambiguous'
    return 'unknown'


def classify_clause(text, *, page_tag=None, source_section=None, attribute=None):
    """Type a clause by source position and grammatical subject, never a score."""
    policy = clause_policy()
    position = _position(page_tag, source_section)
    actor = re.search(policy['actor_subject_pattern'], text or '', re.IGNORECASE)
    if actor:
        if position == 'atc' and any(re.search(pattern, text, re.IGNORECASE)
                                     for pattern in policy['eligibility_action_patterns']):
            return ClauseClassification('eligibility', position,
                'source qualification section and bidder/supplier subject with experience or possession obligation')
        return ClauseClassification('unknown', position,
            'actor-subject obligation has no unambiguous source qualification context', True)
    if re.search(policy['ambiguous_subject_pattern'], text or '', re.IGNORECASE):
        return ClauseClassification('unknown', position, 'clause subject is an unresolved reference', True)
    if re.search(policy['competing_actor_pattern'], text or '', re.IGNORECASE):
        return ClauseClassification('unknown', position, 'source clause mixes product and actor-subject context', True)
    words = policy['retype']['attribute_words']
    property_words = [word for word in words if re.search(rf'(?<![a-z]){re.escape(word)}(?![a-z])', text or '', re.IGNORECASE)]
    property_present = bool(property_words or (attribute and attribute not in policy['non_property_attributes']))
    if property_present and position == 'specification' and not reads_as_legal_clause(text):
        return ClauseClassification('technical', position, 'specification position with a product-property subject')
    prefix = policy['attribute_prefix_pattern']
    labelled_property = any(re.search(prefix + re.escape(word) + r'(?![a-z])', text or '', re.IGNORECASE)
                            for word in property_words)
    product_obligation = re.search(policy['product_obligation_pattern'], text or '', re.IGNORECASE)
    if property_present and position == 'atc' and (labelled_property or product_obligation) and not reads_as_legal_clause(text):
        return ClauseClassification('technical', position, 'buyer-term position with a product-property obligation')
    return ClauseClassification('unknown', position,
        'source position or subject does not establish a technical or eligibility clause', True)

# The page kinds. A page is exactly one of these; `mixed` means it carries
# technical content *and* one of the non-technical kinds, so it is read.
TECHNICAL = "technical"
COMMERCIAL_LEGAL = "commercial_legal"
SAFETY_HSE = "safety_hse"
BOILERPLATE = "boilerplate"
MIXED = "mixed"

# The kinds whose clauses are recorded but never sent to the model (D-87).
DISCOVERY = "catalogue_discovery"
WRAPPER_METADATA = "wrapper_metadata"
NON_TECHNICAL = frozenset({COMMERCIAL_LEGAL, SAFETY_HSE, BOILERPLATE, DISCOVERY, WRAPPER_METADATA})

# The prefix a requirement's `review_reason` carries when its page was not a
# specification. The pipeline writes it, the report counts by it, so the two
# cannot drift apart (D-92).
NON_TECHNICAL_REASON = "page classified as"

# Weight 2 terms say what the page *is*; weight 1 terms only say what it
# mentions. A legal page that says "specification" once must not outvote
# "integrity pact" and "arbitration".
COMMERCIAL_LEGAL_TERMS: dict[str, int] = clause_policy()["page_terms"]["COMMERCIAL_LEGAL_TERMS"]

SAFETY_HSE_TERMS: dict[str, int] = clause_policy()["page_terms"]["SAFETY_HSE_TERMS"]

BOILERPLATE_TERMS: dict[str, int] = clause_policy()["page_terms"]["BOILERPLATE_TERMS"]

TECHNICAL_TERMS: dict[str, int] = clause_policy()["page_terms"]["TECHNICAL_TERMS"]

# A number with a unit is the fallback signal for a page that names nothing
# else: a specification table of bare values is still technical.
NUMBER_WITH_UNIT = re.compile(clause_policy()["numeric_fallback_pattern"], re.IGNORECASE)

# How many number+unit pairs make an otherwise keyword-free page technical.
NUMERIC_FALLBACK_MIN = 2


@dataclass
class PageClassification:
    """What the page is, and the evidence for it."""

    kind: str
    scores: dict[str, int] = field(default_factory=dict)
    matched: dict[str, list[str]] = field(default_factory=dict)

    @property
    def is_technical(self) -> bool:
        return self.kind not in NON_TECHNICAL


def _score(text: str, terms: dict[str, int]) -> tuple[int, list[str]]:
    """The weighted score of a family, and the terms that earned it.

    Each term counts once, however often the page repeats it: a page that
    says "safety" twenty times is not twenty times as safe.
    """
    lowered = text.lower()
    score = 0
    matched: list[str] = []
    for term, weight in terms.items():
        if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lowered):
            score += weight
            matched.append(term)
    return score, matched


def classify_page(text: str) -> PageClassification:
    """Tag one page from its text.

    The rule is dominance, not a fixed threshold: a page that names more
    technical terms than legal ones is technical, and a page that names both
    in real quantity is `mixed` and is read. A single incidental word does
    not make a legal page technical.
    """
    commercial, commercial_hits = _score(text, COMMERCIAL_LEGAL_TERMS)
    safety, safety_hits = _score(text, SAFETY_HSE_TERMS)
    boilerplate, boilerplate_hits = _score(text, BOILERPLATE_TERMS)
    technical, technical_hits = _score(text, TECHNICAL_TERMS)

    scores = {
        COMMERCIAL_LEGAL: commercial,
        SAFETY_HSE: safety,
        BOILERPLATE: boilerplate,
        TECHNICAL: technical,
    }
    matched = {
        COMMERCIAL_LEGAL: commercial_hits,
        SAFETY_HSE: safety_hits,
        BOILERPLATE: boilerplate_hits,
        TECHNICAL: technical_hits,
    }

    other = commercial + safety + boilerplate

    if technical == 0 and other == 0:
        kind = _numeric_fallback(text)
    elif other == 0:
        kind = TECHNICAL
    elif technical == 0:
        kind = _dominant(commercial, safety, boilerplate)
    elif technical >= 2 and other >= 2:
        # Both are present in real quantity. The page must be read, but the
        # legal half must not be read as requirements - the tag says so.
        kind = MIXED
    elif technical > other:
        # A single incidental legal word does not make a spec page legal.
        kind = TECHNICAL
    else:
        kind = _dominant(commercial, safety, boilerplate)

    return PageClassification(kind=kind, scores=scores, matched=matched)


def _dominant(commercial: int, safety: int, boilerplate: int) -> str:
    """The strongest non-technical family, with a fixed tie order.

    The order matters only for a tie, and a legal page that also mentions
    safety is still a legal page: the integrity pact is the one that carries
    the obligations.
    """
    ranked = [
        (commercial, COMMERCIAL_LEGAL),
        (safety, SAFETY_HSE),
        (boilerplate, BOILERPLATE),
    ]
    return max(ranked, key=lambda pair: pair[0])[1]


def _numeric_fallback(text: str) -> str:
    """A page with no keywords at all: values make it technical.

    A specification table can be bare numbers and units. A page of letterhead
    or an empty scan is not.
    """
    if len(NUMBER_WITH_UNIT.findall(text)) >= NUMERIC_FALLBACK_MIN:
        return TECHNICAL
    return BOILERPLATE


def is_non_technical(kind: str) -> bool:
    """True when the page's clauses must not be sent to the model."""
    return kind in NON_TECHNICAL


# How many weighted non-technical hits make a *clause* an obligation. A page
# carries hundreds of words, so dominance is enough there; a clause carries
# twenty, and one incidental word ("contract", "warranty") is as likely to be
# a commercial rider on a specification as a purely commercial clause.
CLAUSE_LEGAL_MIN = 2


def reads_as_legal_clause(text: str) -> bool:
    """True when a clause states an obligation, not a specification.

    A `mixed` page is read as a whole, so the page tag cannot separate the two
    halves: "to be submitted on the company's letter head by the authorised
    signatory" sits on the same page as a size table. This reads the clause on
    its own words, with the same weighted terms, and answers the one question
    the re-typing needs: is there a capability in here at all?

    A tie goes to the obligation. The words that tie are words the two
    families share - "head" is both a pump's head and a letter head - and the
    cost of reading a letterhead as a specification is a comparison nobody
    asked for, while the cost of the other error is a clause that is still
    reported as unread and reviewed.
    """
    legal = (
        _score(text, COMMERCIAL_LEGAL_TERMS)[0]
        + _score(text, SAFETY_HSE_TERMS)[0]
        + _score(text, BOILERPLATE_TERMS)[0]
    )
    if legal < CLAUSE_LEGAL_MIN:
        return False
    technical = _score(text, TECHNICAL_TERMS)[0]
    return legal >= technical
