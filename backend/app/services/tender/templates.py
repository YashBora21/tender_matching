"""Read-only template discovery; repeated text never approves its own exclusion."""
import argparse
from collections import defaultdict
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import unicodedata


def normalized(text):
    return ' '.join(unicodedata.normalize('NFKC', text or '').casefold().split())


def templates_path():
    return Path(__file__).resolve().parents[3] / 'data' / 'tender_templates.json'


@lru_cache(maxsize=1)
def reviewed_data():
    # Snapshot policy for a reading process; restart after reviewing policy changes.
    data = json.loads(templates_path().read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('templates'), list):
        raise ValueError('template policy requires a templates list')
    for row in data['templates']:
        if not isinstance(row, dict):
            raise ValueError('template entries must be objects')
        if row.get('approved') is True and not all(
                isinstance(row.get(key), str) and row[key].strip()
                for key in ('template_id', 'normalized_text', 'type')):
            raise ValueError('approved templates require an ID, normalized text and clause type')
        if row.get('approved') is True and not isinstance(row.get('roles'), list):
            raise ValueError('approved templates require explicit source roles')
    return data


def reviewed_template(text, role):
    allowed = {'technical', 'certification', 'commercial', 'eligibility', 'informational'}
    matches = [row for row in reviewed_data()['templates']
               if row.get('approved') is True and row.get('context_independent') is True
               and role in row.get('roles', []) and row.get('type') in allowed
               and row.get('normalized_text') == normalized(text)]
    if len({row['type'] for row in matches}) != 1:
        return None
    return matches[0] if matches else None


def candidates(rows):
    from app.services.tender.source_policy import policy
    groups = defaultdict(dict)
    for row in rows:
        text = normalized(row.get('source_text'))
        if text:
            groups[text].setdefault(str(row['tender_id']), row)
    result = []
    for text, tenders in sorted(groups.items()):
        if len(tenders) < policy()['template_min_distinct_tenders']:
            continue
        result.append({'template_id': sha256(text.encode()).hexdigest(),
            'normalized_text': text, 'distinct_tenders': len(tenders),
            'approved': False, 'context_independent': False, 'type': None, 'roles': [],
            'examples': [{'tender_id': tender_id, 'requirement_id': str(row['id']),
                          'filename': row.get('filename'), 'page': row.get('source_page'),
                          'source_table_id': str(row['source_table_id']) if row.get('source_table_id') else None,
                          'source_role': ((row.get('normalization') or {}).get('source_context') or {}).get('source_role', 'unknown'),
                          'source_text': row['source_text']}
                         for tender_id, row in sorted(tenders.items())]})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path,
                        help='Separate candidate JSON file for review; never the approved template file')
    args = parser.parse_args(argv)
    if args.output.resolve() == templates_path().resolve():
        parser.error('candidate discovery cannot overwrite reviewed template policy')
    from app.repositories.db import get_engine
    from app.repositories.schema import requirement, tender
    from sqlalchemy import select
    query = select(requirement.c.id, requirement.c.tender_id, requirement.c.source_text,
                   requirement.c.source_page, requirement.c.source_table_id,
                   requirement.c.normalization, tender.c.filename).join(
                       tender, tender.c.id == requirement.c.tender_id).where(requirement.c.superseded_at.is_(None))
    with get_engine().connect() as connection:
        rows = connection.execute(query).mappings().all()
    found = candidates(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'templates': found}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'candidate_count': len(found), 'distinct_tenders': len({str(row['tender_id']) for row in rows}),
                      'output': str(args.output), 'database_writes': 0, 'provider_calls': 0}))


if __name__ == '__main__':
    main()
