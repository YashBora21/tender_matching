"""Conservative source-facet review trigger, never semantic completeness proof."""
import re
from app.comparison.vocabulary import load_vocabulary, vocabulary_path, normalise_token

POLICY_VERSION = "structured-source-coverage-1"
MARKER = "source_coverage_gap"
_TOKEN = re.compile(r"[A-Za-z]+|[0-9]+(?:\.[0-9]+)?|[<>]=?|[+\-±]")


def uncovered_tokens(text, requirements):
    vocabulary=load_vocabulary(vocabulary_path())
    represented=set()
    approved_names={normalise_token(name) for spec in vocabulary.attributes if spec.approved
                    for name in (spec.canonical_term, *spec.approved_synonyms)}
    for requirement in requirements:
        if normalise_token(requirement.attribute or '') not in approved_names:
            continue
        operator = {'eq': '=', 'ne': '!=', 'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}.get(requirement.operator)
        if operator:
            represented.add(operator)
        for value in (requirement.attribute, requirement.value, requirement.unit):
            represented.update(match.group().casefold() for match in _TOKEN.finditer(value or ''))
    return sorted({match.group().casefold() for match in _TOKEN.finditer(text)
                   if match.group().casefold() not in represented | vocabulary.coverage_ignored_tokens})
