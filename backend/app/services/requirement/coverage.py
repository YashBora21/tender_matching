"""Conservative source-facet review trigger, never semantic completeness proof."""
import re
from app.comparison.vocabulary import load_vocabulary, vocabulary_path, normalise_token

POLICY_VERSION = "structured-source-coverage-3"
MARKER = "source_coverage_gap"
_TOKEN = re.compile(r"[A-Za-z]+|[0-9]+(?:\.[0-9]+)?|[<>]=?|[+\-±]")


def uncovered_tokens(text, requirements, *, source_number=None):
    vocabulary=load_vocabulary(vocabulary_path())
    represented=set()
    approved_names={normalise_token(name) for spec in vocabulary.attributes if spec.approved
                    for name in (spec.canonical_term, *spec.approved_synonyms)}
    for requirement in requirements:
        approved = normalise_token(requirement.attribute or '') in approved_names
        from app.services.requirement.evidence import labelled_field_supported
        anchored = labelled_field_supported(text, requirement.source_span, requirement.attribute,
                                            requirement.value, getattr(requirement, 'table_context', None))
        if not approved:
            # Reading a quoted open-vocabulary field is separate from approving its mapping.
            from app.services.requirement.evidence import span_supports
            span = requirement.source_span
            if span is None or not requirement.value or not span_supports(text, span, requirement.value):
                continue
        operator = {'eq': '=', 'ne': '!=', 'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}.get(requirement.operator)
        if operator:
            represented.add(operator)
        for value in ((requirement.attribute if approved or anchored else None), requirement.value, requirement.unit):
            represented.update(match.group().casefold() for match in _TOKEN.finditer(value or ''))
        # The parser's explicit item identifier is metadata when the rest of the row is fully read.
        prefix = str(source_number or '') + ' '
        if source_number and text.startswith(prefix) and requirement.source_span is not None:
            from app.services.requirement.evidence import span_supports, quote_span
            span = requirement.source_span
            fields = ' '.join(filter(None,[(requirement.attribute if approved or anchored else None),
                                           requirement.value,requirement.unit]))
            tokens = lambda value: [m.group().casefold() for m in _TOKEN.finditer(value)]
            if (span_supports(text,span,requirement.value) and requirement.value
                    and quote_span(text,text[span[0]:span[1]]) == tuple(span)
                    and tokens(text[len(prefix):]) == tokens(fields)):
                represented.update(tokens(str(source_number)))
    return sorted({match.group().casefold() for match in _TOKEN.finditer(text)
                   if match.group().casefold() not in represented | vocabulary.coverage_ignored_tokens})
