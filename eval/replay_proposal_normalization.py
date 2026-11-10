"""Offline replay of frozen proposals; never calls a provider or scores accuracy."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

import pymupdf
from app.services.requirement.retype import retype
from app.services.requirement.structured_values import normalize_records, fingerprint
from app.services.tender.classify import classification_policy_fingerprint
from app.services.tender.ingest import RequirementRecord
from app.schemas.enums import RequirementType


def replay(manifest_path):
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for name, expected in manifest['files'].items():
        assert sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    document = pymupdf.open(ROOT / manifest['source_pdf'])
    page = document[manifest['page'] - 1].get_text()
    rows = []
    changes = Counter()
    abstentions = Counter()
    for case in manifest['cases']:
        source = page.split(case['start'], 1)[1].split(case['end'], 1)[0]
        source = case['start'] + source
        proposals_path = ROOT / case['proposal']
        immutable_bytes = proposals_path.read_bytes()
        for index, raw in enumerate(json.loads(immutable_bytes)['requirements']):
            quote = raw['source_quote']
            assert ' '.join(quote.split()) in ' '.join(source.split()), (case['item_id'], quote)
            record = RequirementRecord(text=raw['text'], source_text=source,
                item_id=case['item_id'], page=manifest['page'], page_tag='technical',
                source_section=manifest['section'], requirement_type=RequirementType(raw['type']),
                attribute=raw['attribute'], value=raw['value'], unit=raw['unit'],
                operator=raw['operator'], mandatory=raw['mandatory'])
            retype(record)
            normalize_records([record])
            assert (record.attribute, record.value, record.unit) == (raw['attribute'], raw['value'], raw['unit'])
            assert record.source_text == source
            normalized = record.normalization
            changed = [key for key in ('attribute', 'value', 'unit') if normalized[key] != raw[key]]
            for key in changed:
                changes[key] += 1
            if record.requirement_type.value != raw['type']:
                changes['type'] += 1
            causes = []
            if normalized['review_required']:
                causes.append(normalized['review_reason'])
            if record.review_required and not causes:
                causes.append(record.review_reason)
            for cause in causes:
                abstentions[cause] += 1
            rows.append({'item_id':case['item_id'], 'proposal_row':index + 1,
                'raw':raw, 'quote_sha256':sha256(quote.encode()).hexdigest(),
                'source_block_sha256':sha256(source.encode()).hexdigest(),
                'derived':normalized, 'type_before':raw['type'], 'type_after':record.requirement_type.value,
                'changed_fields':changed, 'comparison_input_eligible':not causes,
                'abstention_causes':causes, 'verification_outcome':'not_run'})
        assert proposals_path.read_bytes() == immutable_bytes
    return {'date':'2026-11-10', 'scope':manifest['scope'],
        'classification_policy':classification_policy_fingerprint(), 'normalization_policy':fingerprint(),
        'source_hashes_asserted':manifest['files'], 'raw_quotes_preserved':True,
        'rows':rows, 'total_rows':len(rows), 'changed_derived_fields':dict(changes),
        'rows_with_changed_derived_fields':sum(bool(row['changed_fields']) for row in rows),
        'rows_with_changed_type':sum(row['type_before'] != row['type_after'] for row in rows),
        'comparison_input_eligible':sum(row['comparison_input_eligible'] for row in rows),
        'abstentions_by_cause':dict(abstentions), 'confirmed_matches':None,
        'false_confident_claims':None, 'gold_accuracy':None, 'llm_calls':0, 'cost_inr':0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = replay(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({key:value for key,value in output.items() if key not in ('rows', 'source_hashes_asserted')}))
