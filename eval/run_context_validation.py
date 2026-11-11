"""Bounded public-corpus validation. Writes proposals/results, never fact rows.

Run from backend. Existing output is refused to avoid overwriting evidence.
"""
import json,os,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.repositories.db import get_engine
from app.repositories.tender import TenderRepo
from app.services.llm import LLMClient,LLMRateLimited,LLMBudgetExceeded
from app.services.match.facts import extract_fact
from app.services.match.match import match_requirement
from app.services.match.verify import check_source_anchor,check_range_endpoint,check_citation
from app.services.output.runs import _assess,assessable

OUT=ROOT/'eval/audit/context-validation.json'

def save(data):
    OUT.write_text(json.dumps(data,indent=2,default=str),encoding='utf-8')

def main():
    if OUT.exists() and '--resume' not in sys.argv:
        raise RuntimeError('Use --resume to continue saved validation evidence')
    client=LLMClient()
    data={'proposals':[],'benchmark':[],'human_accuracy':None,'database_writes':0}
    if OUT.exists():
        data=json.loads(OUT.read_text(encoding='utf-8'))
        data.pop('stop',None)
        for attempt in data['benchmark']:
            invalid=[r for r in attempt['results'] if 'budget' in (r.get('review_reason') or '')]
            if invalid:
                attempt.setdefault('discarded_budget_abstentions',[]).extend(invalid)
                attempt['results']=[r for r in attempt['results'] if r not in invalid]
                attempt['complete']=False
                attempt['prior_budget_pass_seconds']=attempt.get('seconds',0)
                attempt['seconds']=0
    source=json.loads((ROOT/'eval/audit/source-context-reassessment.json').read_text(encoding='utf-8'))
    groups={}
    for f in source['facts']:
        c=f['source_context']; claim=c['claim']
        key=(c['document_id'],c['page'],claim.get('attribute'))
        groups.setdefault(key,[]).append(f)
    for key,facts in groups.items():
        if any(p['source_fact_ids']==[f['fact_id'] for f in facts] for p in data['proposals']):
            continue
        c=facts[0]['source_context']; attr=c['claim'].get('attribute') or 'pressure class'
        requirement='Documented '+attr.replace('_',' ')
        # Use the full local page as bounded source evidence, not only the old fragment.
        from app.services.match.match import _corpus_file
        import pymupdf
        with get_engine().connect() as conn:
            conn.execute(text('SET TRANSACTION READ ONLY'))
            path=conn.execute(text('select path from document where id::text=:id'),{'id':c['document_id']}).scalar_one()
            conn.rollback()
        with pymupdf.open(_corpus_file(path)) as pdf:
            c['passage']=pdf[c['page']-1].get_text()
        before=client.calls; started=time.perf_counter()
        try:
            result=extract_fact(requirement,c['passage'],client=client,source_context=c)
            proposals=[]
            for fact in result.facts:
                context=dict(c); context['claim']={'model_or_series':fact.model,'conditions':fact.conditions,
                    'table_index':fact.table_index,'row_index':fact.row_index,'column_index':fact.column_index}
                anchor=check_source_anchor(context,fact.attribute,fact.value)
                endpoint=check_range_endpoint(fact.quote,fact.value)
                proposals.append({'fact':fact.model_dump(),'anchor_error':anchor.reason if anchor else None,
                    'range_error':endpoint.reason if endpoint else None,
                    'citation_passed':check_citation(fact.quote,c['passage']).passed if fact.found else None})
            data['proposals'].append({'source_fact_ids':[f['fact_id'] for f in facts],
                'error':result.error,'proposals':proposals,'calls':client.calls-before,'seconds':time.perf_counter()-started})
        except (LLMRateLimited,LLMBudgetExceeded) as exc:
            data['stop']=type(exc).__name__
            data['retry_after']=getattr(exc,'retry_after',None)
            data['stop_detail']=str(exc)
            save(data)
            print(json.dumps({'stop':data['stop'],'calls':client.calls,'retry_after':data['retry_after'],'detail':str(exc)}),flush=True)
            return
        save(data)
        print(json.dumps({'proposal_groups':len(data['proposals']),'calls':client.calls}),flush=True)
    tender='2bc8f917-262d-4ea8-a1c9-b2997ef1f256'
    for batch in ('1','2'):
        os.environ['MATCH_EXTRACTION_BATCH_SIZE']=batch
        os.environ['MATCH_MAX_CANDIDATES']='2'
        attempt=next((a for a in data['benchmark'] if a['batch_size']==int(batch)),None)
        if attempt and attempt.get('complete'):
            continue
        if attempt is None:
            attempt={'batch_size':int(batch),'tender_id':tender,'manufacturer':'Flowserve','results':[],'complete':False}
            data['benchmark'].append(attempt)
        attempt.pop('stop',None)
        prior={key:attempt.get(key,0) for key in ('seconds','calls','tokens','retries')}
        started=time.perf_counter(); before=(client.calls,client.tokens,client.retries)
        with Session(get_engine()) as session:
            repo=TenderRepo(session)
            requirements=assessable(repo.list_requirements(tender))
            items={i['item_id']:i for i in repo.list_items(tender)}
            session.info['requirement_embeddings']={}
            attempt['expected']=len(requirements)
            for r in requirements:
                if any(str(p['requirement_id'])==str(r['id']) for p in attempt['results']):
                    continue
                try:
                    result,category=_assess(session,r,items,manufacturer_name='Flowserve',matcher=match_requirement,client=client)
                    attempt['results'].append({'requirement_id':r['id'],'item_id':r.get('item_id'),
                        'status':result.status.value,'verification':result.verification_outcome.value,
                        'review_required':result.review_required,'review_reason':result.review_reason,
                        'quote':result.quote,'source_context':result.source_context,'stage_seconds':result.stage_seconds})
                except (LLMRateLimited,LLMBudgetExceeded) as exc:
                    attempt['stop']=type(exc).__name__
                    attempt['retry_after']=getattr(exc,'retry_after',None)
                    break
                except Exception as exc:
                    attempt['results'].append({'requirement_id':r['id'],'error':type(exc).__name__})
                save(data)
            attempt.update(seconds=prior['seconds']+time.perf_counter()-started,calls=prior['calls']+client.calls-before[0],
                           tokens=prior['tokens']+client.tokens-before[1],retries=prior['retries']+client.retries-before[2])
            attempt['complete']=len(attempt['results'])==len(requirements) and not any('error' in r for r in attempt['results'])
            session.rollback()
        save(data)
        print(json.dumps({k:v for k,v in attempt.items() if k!='results'}),flush=True)
        if attempt.get('stop'): break
    data['last_process_calls']=client.calls; data['last_process_tokens']=client.tokens
    data['counter_scope']='Last process counters only; use per-pass counters for resumed matching totals. Failed proposal attempts before resumption are not fully aggregated.'
    save(data)

if __name__=='__main__': main()
