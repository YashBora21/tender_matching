"""Export source-bound facts for human review; never generate gold labels.

Database is read-only. Run from backend with ../venv/Scripts/python.exe ../eval/evidence_review.py.
"""
import csv
import hashlib
import json
import sys
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

import pymupdf
from sqlalchemy import text
from app.repositories.db import get_engine
from app.services.match.match import _corpus_file
from app.services.match.verify import check_citation, check_table_binding


def inspect_fact(row, pages):
    result = dict(row)
    result.update(machine_issue='', table_count=0, citation_passed=False,
                  human_attribute_correct='', human_value_correct='',
                  human_source_scope_correct='', human_notes='')
    path = _corpus_file(row['path'])
    if path is None or not path.exists():
        result['machine_issue'] = 'source_missing'
        return result
    if hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
        result['machine_issue'] = 'source_hash_mismatch'
        return result
    key = (row['document_id'], row['page'])
    try:
        if key not in pages:
            with pymupdf.open(path) as pdf:
                if not row['page'] or not 1 <= row['page'] <= len(pdf):
                    result['machine_issue'] = 'page_out_of_bounds'
                    return result
                page = pdf[row['page']-1]
                pages[key] = (page.get_text(), [t.extract() for t in page.find_tables().tables])
        page_text, tables = pages[key]
        result['table_count'] = len(tables)
        result['citation_passed'] = check_citation(row['quote'], page_text).passed
        binding = check_table_binding(tables, row['attribute'], row['value'])
        if not result['citation_passed']:
            result['machine_issue'] = 'citation_not_verified'
        elif binding:
            result['machine_issue'] = 'wrong_column'
        else:
            result['machine_issue'] = 'not_rejected_requires_human_review'
    except Exception as exc:
        result['machine_issue'] = 'source_inspection_failed:' + type(exc).__name__
    return result


def main():
    with get_engine().connect() as conn:
        conn.execute(text('SET TRANSACTION READ ONLY'))
        rows = conn.execute(text("""
          select f.id::text fact_id,f.document_id::text document_id,m.name manufacturer,
            p.model,a.canonical_term attribute,f.original_name,f.value,f.unit,f.page,
            f.quote,f.verification_status,d.path,d.sha256
          from fact f join product p on p.id=f.product_id
          join manufacturer m on m.id=p.manufacturer_id
          join document d on d.id=f.document_id
          left join attribute a on a.id=f.attribute_id
          where f.verification_status='verified'
            or (m.name='Flowserve' and a.canonical_term='nominal_size')
          order by m.name,p.model,f.page,f.id
        """)).mappings().all()
        conn.rollback()
    pages = {}
    reviewed = [inspect_fact(row, pages) for row in rows]
    out = ROOT / 'eval' / 'audit'
    out.mkdir(exist_ok=True)
    with (out/'evidence-review.csv').open('w',newline='',encoding='utf-8') as file:
        writer = csv.DictWriter(file,fieldnames=list(reviewed[0]) if reviewed else [])
        writer.writeheader()
        writer.writerows(reviewed)
    summary = {'rows':len(reviewed),'unique_pages':len(pages),
        'issues':dict(Counter(r['machine_issue'] for r in reviewed)),
        'human_labels':0,'gold_accuracy':None,
        'verified_rejections':[r['fact_id'] for r in reviewed if r['verification_status']=='verified' and r['machine_issue'] in ('wrong_column','citation_not_verified','source_hash_mismatch','page_out_of_bounds')]}
    (out/'evidence-review-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary),flush=True)


if __name__ == '__main__':
    main()
