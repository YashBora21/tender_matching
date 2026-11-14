"""Apply the explicitly reviewed fact quarantine, preserving a recovery record."""
import argparse
import csv
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from sqlalchemy import text
from app.repositories.db import get_engine


def quarantine_plan(rows, proposals):
    from app.services.cli.fact_cache import digest
    by_id = {p['fact_id']: p for p in proposals}
    if len(by_id) != len(proposals) or set(by_id) != {r['fact_id'] for r in rows}:
        raise ValueError('Audit identities do not match the database')
    changes = []
    for row in rows:
        proposal = by_id[row['fact_id']]
        if proposal['original_fact_sha256'] != digest(row):
            raise ValueError('Stale fact snapshot; rerun the audit before applying')
        if row['verification_status'] == 'verified' and proposal['proposed_status'] == 'needs_review':
            if proposal.get('cache_eligible_after_reviewed_apply') or not proposal.get('blockers'):
                raise ValueError('Quarantine requires failed evidence gates')
            changes.append((row, proposal))
    return changes


def apply_current(audit_dir, output_dir, *, yes=False):
    from copy import deepcopy
    from hashlib import sha256
    from app.services.cli.fact_cache import main as audit_main, digest
    from app.services.match.verify import verify
    summary = json.loads((audit_dir/'summary.json').read_text(encoding='utf-8'))
    proposals = json.loads((audit_dir/'facts.json').read_text(encoding='utf-8'))
    for key, path in [('auditor_code_sha256', Path(audit_main.__code__.co_filename)),
                      ('verification_code_sha256', Path(verify.__code__.co_filename))]:
        if sha256(path.read_bytes()).hexdigest() != summary[key]:
            raise ValueError('Audit/verifier code changed; rerun the audit')
    output_dir.mkdir(parents=True, exist_ok=False)
    query = next(q for q in audit_main.__code__.co_consts if isinstance(q,str) and q.startswith('SELECT f.id::text fact_id'))
    with get_engine().begin() as conn:
        if not yes:
            conn.execute(text('SET TRANSACTION READ ONLY'))
        conn.execute(text('SET LOCAL statement_timeout=30000'))
        rows = [dict(r) for r in conn.execute(text(query + (' FOR UPDATE OF f' if yes else ''))).mappings()]
        if digest(rows) != summary['snapshot_sha256']:
            raise ValueError('Database snapshot changed; rerun the audit')
        changes = quarantine_plan(rows, proposals)
        plan = {'mode':'apply' if yes else 'dry_run', 'promotions':0,
                'quarantines':len(changes), 'snapshot_sha256':summary['snapshot_sha256'],
                'fact_ids':[row['fact_id'] for row, _ in changes]}
        (output_dir/'plan.json').write_text(json.dumps(plan,indent=2)+'\n',encoding='utf-8')
        (output_dir/'recovery.json').write_text(json.dumps(rows,default=str,indent=2)+'\n',encoding='utf-8')
        for row, proposal in changes if yes else []:
            context = deepcopy(row.get('source_context') or {})
            history = context.setdefault('evidence_review_history', [])
            history.append({'recorded_at':datetime.now(timezone.utc).isoformat(),
                'action':'quarantine', 'previous_fact':deepcopy(row),
                'blockers':proposal['blockers'], 'human_review':proposal.get('human_review'),
                'audit_snapshot_sha256':summary['snapshot_sha256']})
            result = conn.execute(text("UPDATE fact SET verification_status='needs_review', source_context=CAST(:context AS jsonb) WHERE id::text=:id AND verification_status='verified'"),
                {'id':row['fact_id'],'context':json.dumps(context,default=str)})
            if result.rowcount != 1:
                raise RuntimeError('Fact changed during quarantine; transaction rolled back')
    (output_dir/'completed.json').write_text(json.dumps(dict(plan,database_writes=len(changes) if yes else 0),indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'mode':plan['mode'],'quarantines':len(changes),'promotions':0}))

QUARANTINE = {
 '2021ac72-c034-4fa0-b85d-2ded7e6c7ad9':'AVK source names Series 55; linked product names Series 06/71/820',
 'e28b7dd7-145d-411c-afb8-1bfb3febdffe':'AVK source names Series 55; linked product names Series 06/71/820',
 'd458a6f2-fb7f-4ba6-90f5-bb0025eeee00':'RF15-specific class linked to broad ball-valve portfolio',
 '2ac85f7b-e444-4964-b2cb-5d2f9c0fcced':'No canonical attribute',
 '3f41ad6a-b847-4467-a2f6-7313f035c039':'No canonical attribute',
 '68f08a55-d0ca-4896-9a29-ca8bd7bcf30a':'No canonical attribute',
 'a1ecf725-a1d5-4636-b5ca-4bbc2679be7a':'No canonical attribute',
 '6d525eb1-66cb-474b-a271-decf9ddd9d0b':'Scalar 10 truncates source range 10-16 inches',
}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yes',action='store_true',help='Apply the selected quarantine; --audit-dir defaults to dry run')
    parser.add_argument('--audit-dir',type=Path,help='Current reviewed dry-run directory; quarantine only, never promote')
    parser.add_argument('--output-dir',type=Path,help='New private directory for plan and recovery record')
    args=parser.parse_args(argv)
    if args.audit_dir or args.output_dir:
        if not (args.audit_dir and args.output_dir):
            parser.error('--audit-dir and --output-dir are required together')
        return apply_current(args.audit_dir,args.output_dir,yes=args.yes)
    if not args.yes:
        raise SystemExit('No database changes made. First run reassess_source_context.py --output-dir PATH and review its dry-run report. Historical quarantine application requires --yes.')
    source=ROOT/'eval/audit/evidence-review.csv'
    with source.open(encoding='utf-8-sig',newline='') as f:
        labels=list(csv.DictReader(f))
    assert len(labels)==28 and all(r['human_attribute_correct'] for r in labels)
    record=ROOT/'eval/audit/human-review-quarantine-2026-11-05.json'
    if record.exists():
        raise RuntimeError('Recovery record already exists; refusing to overwrite')
    with get_engine().begin() as conn:
        rows=conn.execute(text('select * from fact where id::text = ANY(:ids) for update'),{'ids':list(QUARANTINE)}).mappings().all()
        assert len(rows)==len(QUARANTINE)
        assert all(r['verification_status']=='verified' for r in rows)
        record.write_text(json.dumps({'recorded_at':datetime.now(timezone.utc).isoformat(),
            'source':'User-completed evidence-review.csv; retained unchanged',
            'human_labels':labels,'reasons':QUARANTINE,'before':[dict(r) for r in rows]},default=str,indent=2),encoding='utf-8')
        conn.execute(text("update fact set verification_status='needs_review' where id::text = ANY(:ids)"),{'ids':list(QUARANTINE)})
    print(json.dumps({'quarantined':len(rows),'recovery_record':str(record)}))

if __name__=='__main__': main()
