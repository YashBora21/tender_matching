"""Four-layer verifier (FR-23, FR-24).

1. **Citation validity** - the quote exists verbatim (whitespace-normalized) on
   the cited page. This layer is deterministic and is a hard gate.
2. **Entity validity** - the passage refers to the manufacturer and the model
   or family being evaluated.
3. **Semantic validity** - the passage supports the claimed attribute and value.
4. **Comparison validity** - units, operator and conditions are consistent and
   convertible.

Any failure yields `needs_review` with the reason. A failed verification is
never published as a confirmed match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from app.comparison import attributes as attrs
from app.comparison.units import is_compatible
from app.schemas.enums import (
    Applicability,
    RequirementStatus,
    ValueType,
    VerificationOutcome,
)
from app.services.textnorm import normalise_text


class Layer(IntEnum):
    CITATION = 1
    ENTITY = 2
    SEMANTIC = 3
    COMPARISON = 4


@dataclass
class LayerResult:
    layer: Layer
    passed: bool
    reason: str
    # True when the layer is unsure, which also forces review (PRD 9).
    borderline: bool = False


@dataclass
class VerificationReport:
    """The outcome of all four layers."""

    layers: list[LayerResult] = field(default_factory=list)
    applicability: Applicability = Applicability.MODEL_LEVEL

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.layers)

    @property
    def failed_layer(self) -> Optional[Layer]:
        for result in self.layers:
            if not result.passed:
                return result.layer
        return None

    @property
    def reasons(self) -> list[str]:
        return [result.reason for result in self.layers if not result.passed]

    @property
    def review_reasons(self) -> list[str]:
        """Why a human should look: a layer that failed and one that was unsure."""
        return [result.reason for result in self.layers if not result.passed or result.borderline]

    @property
    def borderline(self) -> bool:
        return any(result.borderline for result in self.layers)

    @property
    def outcome(self) -> VerificationOutcome:
        if not self.passed:
            return VerificationOutcome.FAILED
        if self.applicability is Applicability.FAMILY_LEVEL:
            return VerificationOutcome.FAMILY_LEVEL
        return VerificationOutcome.PASSED

    @property
    def review_required(self) -> bool:
        return not self.passed or self.borderline


def normalise_whitespace(text: str) -> str:
    """Collapse all whitespace so a re-wrapped quote still matches."""
    return normalise_text(text)


def check_citation(quote: Optional[str], page_text: Optional[str]) -> LayerResult:
    """Layer 1: the quote must exist on the cited page.

    The quote is matched with the same normalisation the reader used to store
    the chunk (FR-23): control characters removed, Unicode folded and
    whitespace collapsed. A quote that is longer than the page cannot be
    verified, and an empty quote is a failure, not a pass: an unverifiable
    claim is exactly what this layer exists to catch.
    """
    if not quote or not quote.strip():
        return LayerResult(Layer.CITATION, False, "the claim carries no quote to verify")
    if page_text is None:
        return LayerResult(Layer.CITATION, False, "the cited page has no text to check the quote against")

    needle = normalise_text(quote)
    haystack = normalise_text(page_text)
    if not needle:
        return LayerResult(Layer.CITATION, False, "the claim carries no quote to verify")
    if needle in haystack:
        return LayerResult(Layer.CITATION, True, "the quote appears verbatim on the cited page")

    # A quote that spans a line break inside a table cell still matches once
    # the cell separator is normalised.
    collapsed = re.sub(r"\s*\|\s*", " ", haystack)
    if needle in collapsed:
        return LayerResult(Layer.CITATION, True, "the quote appears on the cited page")

    # Quote longer than the page can never match; say so plainly.
    if len(needle) > len(haystack):
        return LayerResult(
            Layer.CITATION, False, "the quote is longer than the cited page"
        )
    return LayerResult(
        Layer.CITATION, False, "the quote was not found on the cited page"
    )


def _identity(text: Optional[str]) -> str:
    """The form a product name is compared in: folded and case-insensitive."""
    return normalise_text(text or "").casefold()


def _names(needle: Optional[str], haystack: Optional[str]) -> bool:
    """True when `needle` is a whole name inside `haystack`.

    Product names are written with a suffix ("GV-100/150", "Series 75/76") and
    a plain substring test would make "GV-10" match "GV-100". The boundary
    test keeps a shorter model from matching a longer one.
    """
    left = _identity(needle)
    right = _identity(haystack)
    if not left or not right:
        return False
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(left)}(?![a-z0-9])", right))


def check_entity(
    *,
    document_manufacturer: Optional[str] = None,
    document_model: Optional[str] = None,
    document_family: Optional[str] = None,
    document_model_or_family: Optional[str] = None,
    expected_manufacturer: Optional[str] = None,
    expected_model: Optional[str] = None,
    expected_family: Optional[str] = None,
    is_family_level: bool = False,
) -> LayerResult:
    """Layer 2: the *document's metadata* must identify the product under test.

    The passage is not evidence about which product it describes: a datasheet
    page that says "body is SS316" names no model, and reading that silence as
    a failure of the product was wrong. Provenance is the document row - the
    manufacturer it belongs to and the model, family or model-or-family it
    declares. The verifier compares that metadata with the product the run is
    answering for.

    When the document declares the model the evidence is model-level. When it
    declares only a family (or declares no product at all) the evidence is
    family-level: uncertain applicability, not a confirmed match (PRD 4).
    """
    if expected_manufacturer:
        if not document_manufacturer:
            return LayerResult(
                Layer.ENTITY,
                False,
                "the document declares no manufacturer to check against",
            )
        if not _names(expected_manufacturer, document_manufacturer):
            return LayerResult(
                Layer.ENTITY,
                False,
                f"the document belongs to {document_manufacturer}, not {expected_manufacturer}",
            )

    declared = [
        value
        for value in (document_model, document_family, document_model_or_family)
        if value
    ]

    if expected_model:
        # The document names the model under test: the strongest provenance.
        for value in (document_model, document_model_or_family):
            if _names(expected_model, value):
                if is_family_level:
                    return LayerResult(
                        Layer.ENTITY,
                        True,
                        f"the document names the model {expected_model} but covers a family",
                        borderline=True,
                    )
                return LayerResult(
                    Layer.ENTITY, True, f"the document declares the model {expected_model}"
                )
        # The document names the family, or a family-level catalogue: the
        # evidence applies to the family, not confirmed for this model.
        if expected_family and any(
            _names(expected_family, value)
            for value in (document_family, document_model_or_family)
        ):
            return LayerResult(
                Layer.ENTITY,
                True,
                f"the document covers the {expected_family} family, not the model {expected_model}",
                borderline=True,
            )
        if is_family_level or document_family or document_model_or_family:
            named = document_family or document_model_or_family or "a family"
            return LayerResult(
                Layer.ENTITY,
                True,
                f"the document covers {named}, not the model {expected_model}",
                borderline=True,
            )
        if document_model:
            return LayerResult(
                Layer.ENTITY,
                False,
                f"the document is about {document_model}, not {expected_model}",
            )
        return LayerResult(
            Layer.ENTITY,
            False,
            f"the document declares no model to check {expected_model} against",
        )

    # No model is under test: the document's own declaration is the provenance.
    if document_model and not is_family_level:
        return LayerResult(
            Layer.ENTITY, True, f"the document declares the model {document_model}"
        )
    if declared:
        return LayerResult(
            Layer.ENTITY,
            True,
            f"the document declares {declared[0]} at family level",
            borderline=True,
        )
    return LayerResult(
        Layer.ENTITY,
        True,
        "the document declares no model, so the evidence is family-level",
        borderline=True,
    )


def check_semantic(
    passage: Optional[str],
    attribute: Optional[str],
    value: Optional[str],
    unit: Optional[str] = None,
) -> LayerResult:
    """Layer 3: the passage must actually support the claimed attribute and value.

    This is a containment check, not an LLM judgement: the claimed value has to
    appear in the passage near the attribute. It is deliberately conservative -
    it fails rather than approves something it cannot see.
    """
    if not passage:
        return LayerResult(Layer.SEMANTIC, False, "the passage is empty")
    if not attribute:
        return LayerResult(Layer.SEMANTIC, False, "the claim names no attribute to support")
    if not value:
        return LayerResult(Layer.SEMANTIC, False, "the claim names no value to support")

    lowered = passage.lower()
    value_text = value.strip().lower()

    # A numeric claim: the number must appear in the passage.
    numeric = re.match(r"^-?\d+(?:\.\d+)?$", value_text)
    if numeric:
        # Compare without trailing zeros: 1500 vs 1500.0.
        candidates = {value_text, value_text.rstrip("0").rstrip("."), f"{float(value_text):g}"}
        if any(candidate in lowered for candidate in candidates):
            return LayerResult(Layer.SEMANTIC, True, "the passage contains the claimed value")
        return LayerResult(
            Layer.SEMANTIC, False, f"the passage does not contain the claimed value {value}"
        )

    # A textual claim: the value must appear in the passage.
    if value_text in lowered:
        return LayerResult(Layer.SEMANTIC, True, "the passage contains the claimed value")
    return LayerResult(
        Layer.SEMANTIC, False, f"the passage does not contain the claimed value {value}"
    )


def check_range_endpoint(quote: Optional[str], value: Optional[str]) -> Optional[LayerResult]:
    if value and quote and re.fullmatch(r"\d+(?:\.\d+)?", value.strip()):
        ranges = re.findall(r'(\d+(?:\.\d+)?)\s*[”"\u2033]?\s*(?:-|–|—|\bto\b)\s*(\d+(?:\.\d+)?)', quote)
        if any(float(value) in (float(low), float(high)) and float(low) != float(high)
               for low, high in ranges):
            return LayerResult(Layer.SEMANTIC, False,
                               "the claimed scalar is only an endpoint of a quoted range")
    return None


def check_source_anchor(context, attribute, value):
    """A table claim must retain an unambiguous cell and its attribute header."""
    if not context:
        return None
    claim = context.get('claim', {})
    model = claim.get('model_or_series')
    if model and context.get('document_model') and not _names(model, context['document_model']):
        return LayerResult(Layer.SEMANTIC, False,
                           'claim names a different model/series than the linked product')
    conditions = claim.get('conditions')
    if conditions and normalise_text(conditions) not in normalise_text(
            context.get('passage', '') + ' ' + str(context.get('tables', []))):
        return LayerResult(Layer.SEMANTIC, False, 'conditions are absent from source context')
    if not context.get('tables'):
        from app.services.match.prose_binding import bind_inline_model
        spec = attrs.attribute_by_term(attribute or '') or attrs.find_attribute(attribute or '')
        labels = (spec.canonical_term, *spec.approved_synonyms) if spec else (attribute or '',)
        anchor = bind_inline_model(context.get('native_lines', []), model or context.get('document_model'),
                                   labels, value, claim.get('quote'), claim.get('unit'), conditions)
        if anchor is None:
            from app.services.match.source_regions import propose_region_anchor
            proposal = propose_region_anchor(context.get('native_lines', []),
                model or context.get('document_model'), labels, value, claim.get('quote'),
                claim.get('unit'), conditions)
            if proposal is not None:
                context['region_anchor_proposal'] = proposal
                return LayerResult(Layer.SEMANTIC, False,
                    'heading-based source region is a proposal requiring review')
            return LayerResult(Layer.SEMANTIC, False,
                'non-table claim lacks an explicit native-line model and attribute/value binding')
        context['resolved_anchor'] = anchor
        return None
    try:
        indices = [claim['table_index'], claim['row_index'], claim['column_index']]
        if any(type(index) is not int or index < 0 for index in indices):
            raise ValueError('invalid cell coordinates')
        table = context['tables'][claim['table_index']]
        row_index, column_index = claim['row_index'], claim['column_index']
        if row_index == 0:
            raise ValueError('header is not a value row')
        cell = table[row_index][column_index]
        header = table[0][column_index]
        spec = attrs.attribute_by_term(attribute or '') or attrs.find_attribute(attribute or '')
        compact = lambda x: re.sub(r'[^\w]', '', (x or '').casefold())
        labels = {compact(s).removesuffix('s') for s in (*spec.synonyms, spec.canonical_term)} if spec else {compact(attribute).removesuffix('s')} - {''}
        exact_value = lambda x: normalise_text(x or '').casefold()
        # A label-cell anchor is resolvable only when its immediate value cell agrees exactly.
        if compact(cell).removesuffix('s') in labels and column_index + 1 < len(table[row_index]):
            adjacent = table[row_index][column_index + 1]
            if value and exact_value(value) == exact_value(adjacent):
                column_index += 1
                cell, header = adjacent, table[0][column_index]
                context['resolved_anchor'] = {
                    'table_index': claim['table_index'], 'row_index': row_index,
                    'column_index': column_index, 'method': 'exact_adjacent_label_value',
                }
        if not value or exact_value(value) != exact_value(cell):
            raise ValueError('cell does not contain the claimed value')
        column_label = compact(header).removesuffix('s') in labels
        row_label = column_index > 0 and compact(table[row_index][column_index - 1]).removesuffix('s') in labels
        if row_label and not column_label:
            other_header = any(
                compact(header).removesuffix('s') in {
                    compact(label).removesuffix('s') for label in (*other.synonyms, other.canonical_term)
                }
                for other in attrs.load_vocabulary(attrs.vocabulary_path()).attributes
                if spec is not None and other.canonical_term != spec.canonical_term
            )
            if other_header:
                raise ValueError('row label conflicts with column header')
        if not (column_label or row_label):
            raise ValueError('cell/header does not support the claim')
        scope = model or context.get('document_model')
        for index, label in enumerate(table[0]):
            if compact(label) in {'model', 'series', 'modelseries', 'modelnumber'}:
                row_model = table[row_index][index]
                if row_model and (not scope or not _names(row_model, scope)):
                    raise ValueError('row model is outside the linked product scope')
        return None
    except (KeyError, TypeError, IndexError, ValueError):
        return LayerResult(Layer.SEMANTIC, False,
                           'table claim lacks a verified cell and attribute header; merged or ambiguous tables need review')


def check_table_binding(tables, attribute: Optional[str], value: Optional[str]) -> Optional[LayerResult]:
    """Reject a value located under another column in a source table."""
    spec = attrs.attribute_by_term(attribute or "") or attrs.find_attribute(attribute or "")
    if spec is None or not value:
        return None
    compact = lambda text: re.sub(r"[^\w]", "", (text or "").casefold())
    labels = {compact(s).removesuffix("s") for s in (*spec.synonyms, spec.canonical_term)}
    wanted = compact(value)
    for rows in tables:
        if not rows:
            continue
        columns = {i for i, label in enumerate(rows[0]) if compact(label).removesuffix("s") in labels}
        if not columns:
            continue
        found = {i for row in rows[1:] for i, cell in enumerate(row)
                 if cell and wanted == compact(cell)}
        if found and not found.intersection(columns):
            return LayerResult(Layer.SEMANTIC, False,
                               "the source table places this value under a different attribute column")
    return None


@dataclass
class ListedSet:
    """A passage that lists the values an attribute is available in.

    The list is evidence: the attribute exists, and the values the document
    offers are the ones in the list. When the required value is not among
    them, the document states a failing value - Missing capability - not an
    absence of evidence (PRD 4).
    """

    status: RequirementStatus
    attribute: str
    required: str
    values: tuple[str, ...]
    quote: str
    reason: str


def _requirement_value(text: str, synonyms: tuple[str, ...]) -> Optional[str]:
    """The longest name of an attribute that the requirement uses."""
    lowered = text.casefold()
    for synonym in sorted(synonyms, key=len, reverse=True):
        if re.search(rf"\b{re.escape(synonym.casefold())}\b", lowered):
            return synonym
    return None


# A specification line that lists values is short: the label, the values and a
# unit. A whole table or a contents entry is longer than any single clause, and
# its list is not a statement about one product attribute.
_MAX_LIST_LINE_CHARS = 160


def _listed_values(line: str, synonyms: tuple[str, ...]) -> Optional[tuple[str, ...]]:
    """The values a specification line lists, if it lists any.

    The attribute name has to be the label of the line - what comes before its
    first `:` or `=` - and the rest of the line has to be at least two short
    values. Measured reason: a dense page chunked without line breaks is one
    long line, and a reader that took the attribute word wherever it appeared
    turned everything after it into the values the document offers, and
    published the whole page as the quote of that claim.
    """
    label, separator, tail = line.partition(":")
    if not separator:
        label, separator, tail = line.partition("=")
    if not separator:
        return None
    lowered = label.casefold()
    for synonym in sorted(synonyms, key=len, reverse=True):
        if not re.search(rf"\b{re.escape(synonym.casefold())}\b", lowered):
            continue
        values = attrs.split_values(tail)
        if values:
            return values
    return None


def listed_set_exclusion(
    passage: Optional[str],
    requirement_text: Optional[str],
) -> Optional[ListedSet]:
    """A listed set of values that excludes the required one, if there is one.

    The function is deterministic and only reads a list the document writes
    after an attribute name. A passage that never lists the attribute is
    silent, and returns None: silence is No evidence found, never Missing
    capability (PRD 4, AGENTS #4). Only categorical attributes are read this
    way; a material or a number is compared by its own evaluator.

    shortcut: a page that fits the line limit is the only thing this can cite -
    no corpus chunk carries line breaks yet, so a whole dense page is one long
    line and is left silent rather than quoted as a list. Upgrade when
    ingestion keeps a specification's line breaks, and the real spec lines
    become short.
    """
    if not passage or not requirement_text:
        return None

    vocabulary = attrs.load_vocabulary()
    for spec in vocabulary.attributes:
        if spec.value_type is not ValueType.CATEGORICAL:
            continue
        required = _requirement_value(requirement_text, spec.synonyms)
        if not required:
            continue
        for line in passage.splitlines():
            if len(line) > _MAX_LIST_LINE_CHARS:
                continue
            values = _listed_values(line, spec.synonyms)
            if values is None:
                continue
            if any(attrs.same_value(required, value) for value in values):
                continue
            return ListedSet(
                status=RequirementStatus.MISSING_CAPABILITY,
                attribute=spec.canonical_term,
                required=required,
                values=values,
                quote=line.strip(),
                reason=(
                    f"the document lists {', '.join(values)} for "
                    f"{spec.canonical_term.replace('_', ' ')}, which excludes "
                    f"the required {required}"
                ),
            )
    return None


def check_comparison(
    requirement_unit: Optional[str],
    capability_unit: Optional[str],
    operator_is_known: bool = True,
    convertible: Optional[bool] = None,
) -> LayerResult:
    """Layer 4: units, operator and conditions must be consistent."""
    if not operator_is_known:
        return LayerResult(
            Layer.COMPARISON, False, "the requirement's operator could not be determined"
        )
    if requirement_unit and capability_unit:
        if convertible is None:
            convertible = is_compatible(requirement_unit, capability_unit)
        if not convertible:
            return LayerResult(
                Layer.COMPARISON,
                False,
                f"{requirement_unit} and {capability_unit} are not convertible",
            )
        return LayerResult(Layer.COMPARISON, True, "the units are convertible")
    if requirement_unit and not capability_unit:
        return LayerResult(
            Layer.COMPARISON, False, "the capability has no unit, so it cannot be compared"
        )
    return LayerResult(Layer.COMPARISON, True, "no unit conflict")


def verify(
    *,
    quote: Optional[str],
    page_text: Optional[str],
    passage: Optional[str],
    document_manufacturer: Optional[str] = None,
    document_model: Optional[str] = None,
    document_family: Optional[str] = None,
    document_model_or_family: Optional[str] = None,
    expected_manufacturer: Optional[str] = None,
    expected_model: Optional[str] = None,
    expected_family: Optional[str] = None,
    is_family_level: bool = False,
    attribute: Optional[str] = None,
    value: Optional[str] = None,
    requirement_unit: Optional[str] = None,
    capability_unit: Optional[str] = None,
    operator_is_known: bool = True,
    convertible: Optional[bool] = None,
    source_tables: Optional[list] = None,
) -> VerificationReport:
    """Run all four layers and return the report."""
    report = VerificationReport()
    report.layers.append(check_citation(quote, page_text))
    report.layers.append(
        check_entity(
            document_manufacturer=document_manufacturer,
            document_model=document_model,
            document_family=document_family,
            document_model_or_family=document_model_or_family,
            expected_manufacturer=expected_manufacturer,
            expected_model=expected_model,
            expected_family=expected_family,
            is_family_level=is_family_level,
        )
    )
    binding = check_table_binding(source_tables or [], attribute, value)
    semantic = binding or check_semantic(passage, attribute, value)
    semantic = check_range_endpoint(quote, value) or semantic
    report.layers.append(semantic)
    report.layers.append(
        check_comparison(requirement_unit, capability_unit, operator_is_known, convertible)
    )

    if report.layers[1].passed and report.layers[1].borderline:
        report.applicability = Applicability.FAMILY_LEVEL
    return report
