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

The keyword lists are the configuration. They are deliberately data, not
logic: adding a word is a one-line change here, and nothing else needs to
know how the score is computed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# The page kinds. A page is exactly one of these; `mixed` means it carries
# technical content *and* one of the non-technical kinds, so it is read.
TECHNICAL = "technical"
COMMERCIAL_LEGAL = "commercial_legal"
SAFETY_HSE = "safety_hse"
BOILERPLATE = "boilerplate"
MIXED = "mixed"

# The kinds whose clauses are recorded but never sent to the model (D-87).
NON_TECHNICAL = frozenset({COMMERCIAL_LEGAL, SAFETY_HSE, BOILERPLATE})

# Weight 2 terms say what the page *is*; weight 1 terms only say what it
# mentions. A legal page that says "specification" once must not outvote
# "integrity pact" and "arbitration".
COMMERCIAL_LEGAL_TERMS: dict[str, int] = {
    "integrity pact": 2,
    "arbitration": 2,
    "arbitrator": 2,
    "jurisdiction": 2,
    "court of law": 2,
    "earnest money": 2,
    "emd": 2,
    "bid security": 2,
    "performance bank guarantee": 2,
    "bank guarantee": 2,
    "liquidated damages": 2,
    "penalty": 2,
    "penalties": 2,
    "forfeit": 2,
    "indemnify": 2,
    "indemnity": 2,
    "gst": 2,
    "payment terms": 2,
    "invoice": 2,
    "purchase order": 2,
    "disqualification": 2,
    "conflict of interest": 2,
    "code of conduct": 2,
    "contract": 1,
    "agreement": 1,
    "bidder": 1,
    "tender fee": 1,
    "price": 1,
    "quotation": 1,
    "delivery period": 1,
    "warranty": 1,
    "guarantee": 1,
    "tax": 1,
    "refund": 1,
    "payment": 1,
}

SAFETY_HSE_TERMS: dict[str, int] = {
    "safety officer": 2,
    "safety supervisor": 2,
    "personal protective equipment": 2,
    "ppe": 2,
    "scaffolding": 2,
    "hot work": 2,
    "permit to work": 2,
    "work permit": 2,
    "helmet": 2,
    "safety harness": 2,
    "first aid": 2,
    "fire extinguisher": 2,
    "accident": 2,
    "injury": 2,
    "hazard": 2,
    "unsafe": 2,
    "lockout": 2,
    "tagout": 2,
    "toolbox talk": 2,
    "height work": 2,
    "excavation": 2,
    "environmental protection": 2,
    "waste disposal": 2,
    "safety": 1,
    "emergency": 1,
    "training": 1,
    "housekeeping": 1,
    "pollution": 1,
    "noise": 1,
    "dust": 1,
    "supervisor": 1,
}

BOILERPLATE_TERMS: dict[str, int] = {
    "table of contents": 2,
    "for office use": 2,
    "authorised signatory": 2,
    "authorized signatory": 2,
    "signature": 2,
    "copyright": 2,
    "disclaimer": 2,
    "this page intentionally": 2,
    "annexure": 1,
    "index": 1,
    "seal": 1,
    "witness": 1,
    "notary": 1,
    "preface": 1,
    "page no": 1,
}

TECHNICAL_TERMS: dict[str, int] = {
    "pressure": 2,
    "rating": 2,
    "rated": 2,
    "material": 2,
    "size": 2,
    "diameter": 2,
    "capacity": 2,
    "flow": 2,
    "head": 2,
    "efficiency": 2,
    "temperature": 2,
    "thickness": 2,
    "voltage": 2,
    "frequency": 2,
    "speed": 2,
    "tolerance": 2,
    "impeller": 2,
    "bearing": 2,
    "gasket": 2,
    "casing": 2,
    "shaft": 2,
    "winding": 2,
    "insulation": 2,
    "test": 1,
    "inspection": 1,
    "specification": 1,
    "technical": 1,
    "standard": 1,
    "grade": 1,
    "valve": 1,
    "pump": 1,
    "motor": 1,
    "compressor": 1,
    "transformer": 1,
    "cable": 1,
    "panel": 1,
    "boiler": 1,
    "pipe": 1,
    "flange": 1,
}

# A number with a unit is the fallback signal for a page that names nothing
# else: a specification table of bare values is still technical.
NUMBER_WITH_UNIT = re.compile(
    r"\d+(?:[.,]\d+)?\s*"
    r"(?:mm|cm|mtr|meter|metre|inch|in\b|kg|gm|ton|bar|psi|kpa|mpa|"
    r"kw|hp|watt|volt|amp|hz|rpm|lpm|lps|m3/h|gpm|%|degree|°c)",
    re.IGNORECASE,
)

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
