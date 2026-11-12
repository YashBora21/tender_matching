"""Conservative source-facet review trigger, never semantic completeness proof."""
import re
from app.comparison.vocabulary import load_vocabulary, vocabulary_path, normalise_token

POLICY_VERSION = "structured-source-coverage-2"
MARKER = "source_coverage_gap"
_TOKEN = re.compile(r"[A-Za-z]+|[0-9]+(?:\.[0-9]+)?|[<>]=?|[+\-±]")


def uncovered_tokens(text, requirements):
    vocabulary=load_vocabulary(vocabulary_path())
    represented=set()
    approved_names={normalise_token(name) for spec in vocabulary.attributes if spec.approved
                    for name in (spec.canonical_term, *spec.approved_synonyms)}
    for requirement in requirements:
        approved = normalise_token(requirement.attribute or '') in approved_names
        if not approved:
            # Reading a quoted open-vocabulary field is separate from approving its mapping.
            from app.services.requirement.evidence import span_supports
            span = requirement.source_span
            if span is None or not requirement.value or not span_supports(text, span, requirement.value):
                continue
        operator = {'eq': '=', 'ne': '!=', 'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}.get(requirement.operator)
        if operator:
            represented.add(operator)
        for value in ((requirement.attribute if approved else None), requirement.value, requirement.unit):
            represented.update(match.group().casefold() for match in _TOKEN.finditer(value or ''))
    return sorted({match.group().casefold() for match in _TOKEN.finditer(text)
                   if match.group().casefold() not in represented | vocabulary.coverage_ignored_tokens})
