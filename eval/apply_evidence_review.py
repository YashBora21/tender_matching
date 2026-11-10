"""Apply the explicitly reviewed fact quarantine, preserving a recovery record."""
import csv
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from sqlalchemy import text
from app.repositories.db import get_engine

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

def main():
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
