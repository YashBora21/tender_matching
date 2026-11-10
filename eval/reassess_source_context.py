"""Snapshot quarantined fact sources without rewriting facts or reviewer labels."""
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.repositories.db import get_engine
from app.services.match.match import _evidence_context
from app.services.match.retrieve import Candidate
from app.services.match.verify import check_source_anchor,check_range_endpoint


def main():
    record=json.loads((ROOT/'eval/audit/human-review-quarantine-2026-11-05.json').read_text(encoding='utf-8'))
    ids=list(record['reasons'])+['b07a2d27-207f-40b8-8f9f-55f1202d5d42','48eb0281-eb2e-48e0-a863-e438dbca4970','70314076-8fe4-4f57-b754-d53de1a2c56e']
    output=[]
    with Session(get_engine()) as session:
        session.execute(text('SET TRANSACTION READ ONLY'))
        rows=session.execute(text('''select f.id::text,f.document_id::text,f.page,f.quote,f.value,
          f.conditions,f.verification_status,a.canonical_term,p.model,p.family,d.model_or_family
          from fact f join product p on p.id=f.product_id join document d on d.id=f.document_id
          left join attribute a on a.id=f.attribute_id where f.id::text=ANY(:ids) order by f.id'''),{'ids':ids}).mappings()
        for row in rows:
            context=_evidence_context(session,Candidate(chunk_id='',document_id=row['document_id'],text=row['quote'] or '',page=row['page'],model=row['model'],family=row['family'],model_or_family=row['model_or_family']))
            context['claim']={'attribute':row['canonical_term'],'value':row['value'],
                              'legacy_conditions':row['conditions'],'quote':row['quote']}
            gate=check_source_anchor(context,row['canonical_term'],row['value'])
            range_gate=check_range_endpoint(row['quote'],row['value'])
            output.append({'fact_id':row['id'],'verification_status':row['verification_status'],
                'source_context':context,'anchor_rejection':gate.reason if gate else None,
                'range_rejection':range_gate.reason if range_gate else None,
                'decision':'remain_quarantined_pending_bound_reextraction'})
        session.rollback()
    target=ROOT/'eval/audit/source-context-reassessment.json'
    target.write_text(json.dumps({'facts':output,'promotions':0,'human_labels_modified':False},indent=2),encoding='utf-8')
    print(json.dumps({'facts':len(output),'promotions':0,'artifact':str(target)}))


if __name__=='__main__': main()
