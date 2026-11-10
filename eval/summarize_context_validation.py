"""Summarize recorded validation without manufacturing labels or rerunning models."""
import json
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    data=json.loads((ROOT/'eval/audit/context-validation.json').read_text(encoding='utf-8'))
    gold=[json.loads(line) for line in (ROOT/'eval/baseline/gold-items-10-11.jsonl').read_text().splitlines() if line.strip()]
    summary={'proposals_source_facts':sum(len(g['source_fact_ids']) for g in data['proposals']),
             'proposal_groups':len(data['proposals']),'promotions':0,'passes':[],
             'accuracy_scope':'Only existing frozen Flowserve pair labels; remaining pairs unlabelled.'}
    for a in data['benchmark']:
        predictions={str(r['requirement_id']):r for r in a['results'] if 'status' in r}
        labels=[g for g in gold if g['manufacturer']==a['manufacturer'] and g['requirement_id'] in predictions]
        scored=sum(predictions[g['requirement_id']]['status']==g['status'] for g in labels)
        summary['passes'].append({k:a.get(k) for k in ('batch_size','complete','expected','seconds','calls','tokens','retries','stop')})
        summary['passes'][-1].update(answered=len(a['results']),status_counts=dict(Counter(r.get('status',r.get('error')) for r in a['results'])),
            review_count=sum(r.get('review_required',False) for r in a['results']),
            existing_label_count=len(labels),existing_labels_correct=scored,
            existing_label_accuracy=scored/len(labels) if labels else None,
            unlabelled=len(predictions)-len(labels),
            discarded_budget_rows=len(a.get('discarded_budget_abstentions',[])))
    if len(data['benchmark'])==2:
        left,right=data['benchmark']
        l={str(r['requirement_id']):r.get('status') for r in left['results']}
        r={str(r['requirement_id']):r.get('status') for r in right['results']}
        summary['status_agreement']=sum(l[k]==r[k] for k in l.keys()&r.keys())
        summary['common_pairs']=len(l.keys()&r.keys())
    (ROOT/'eval/audit/context-validation-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary))

if __name__=='__main__': main()
