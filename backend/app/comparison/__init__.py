"""Comparison registry and evaluators (PRD 6.2 layout: backend/app/comparison/).

Deterministic evaluation only: the LLM extracts, this decides (non-negotiable 1).
"""

from __future__ import annotations

from app.comparison.attributes import (
    AttributeSpec,
    certification_pattern,
    dn_pattern,
    find_attribute,
    ip_pattern,
    material_group,
    material_pattern,
    material_satisfies,
    normalise_token,
)
from app.comparison.registry import (
    REGISTRY,
    ComparisonRequest,
    Evaluation,
    evaluate,
    get_evaluator,
)
from app.comparison.units import convert, is_compatible, parse_unit
from app.comparison.vocabulary import Vocabulary, load_vocabulary

__all__ = [
    "AttributeSpec",
    "ComparisonRequest",
    "Evaluation",
    "REGISTRY",
    "Vocabulary",
    "certification_pattern",
    "convert",
    "dn_pattern",
    "evaluate",
    "find_attribute",
    "get_evaluator",
    "ip_pattern",
    "is_compatible",
    "load_vocabulary",
    "material_group",
    "material_pattern",
    "material_satisfies",
    "normalise_token",
    "parse_unit",
]
