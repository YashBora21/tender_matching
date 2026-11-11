"""Fixed-source read-only diagnostic. Run from repository root.
No generative requests, database writes or production reports; cached local embedding weights required.
This measures a known two-page specification subset, not full-tender extraction/matching accuracy.
"""
import sys, json, time, hashlib, logging, contextlib, io, shutil, os
os.environ["HF_HUB_OFFLINE"]="1"
os.environ["TRANSFORMERS_OFFLINE"]="1"
from pathlib import Path
logging.disable(logging.CRITICAL)
sys.path[:0]=['backend','eval']
from sqlalchemy import text,select,cast,Text,event
from app.repositories.db import session_scope,get_engine
from app.repositories.tender import TenderRepo
from app.repositories.schema import document,chunk
from app.core.config import get_settings
from app.services.tender.ingest import read_specification
from app.services.match.category import infer_type
from app.services.match.match import build_query,match_requirement
from app.services.match.retrieve import search
from app.services.llm import _sentence_transformer,LLMBudgetExceeded
from app.services.output.runs import _assess
from recall import load_labels,DatabaseRanking,evaluate
import argparse
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output-dir',type=Path,default=Path('eval/audit/fixed-incorpus-2026-11-08'))
root=parser.parse_args().output_dir;root.mkdir(parents=True,exist_ok=True)
out={'scope':'frozen two-page specification subset; original full tender source integrity checked separately','remote_calls':0}
started=time.perf_counter()
from app.services.match import page_context
from collections import Counter
cache_counts=Counter();cache_seconds=0.0
original_context_loader=page_context.load_page_context
def measured_context(*args,**kwargs):
 global cache_seconds
 begin=time.perf_counter()
 try:
  result=original_context_loader(*args,**kwargs)
  cache_counts[result['cache']]+=1
  return result
 finally:cache_seconds+=time.perf_counter()-begin
page_context.load_page_context=measured_context

@event.listens_for(get_engine(),'begin')
def readonly_transaction(connection):
 connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
def digest(data):return hashlib.sha256(data).hexdigest()
def save():
 temp=root/'results.tmp';temp.write_text(json.dumps(out,indent=2,default=str),encoding='utf-8');temp.replace(root/'results.json')
def emit(phase,**values):print(json.dumps({'phase':phase,**values}),flush=True)
class NoRemote:
 calls=prompt_tokens=completion_tokens=retries=0
 blocked=0
 def complete_json(self,*args,**kwargs):
  self.blocked+=1
  raise LLMBudgetExceeded('read-only validation has no remote generation budget')
try:
 settings=get_settings();tid='9d62cf12-0d62-4930-8ab3-931c86543096'
 original=Path('backend/data/tenders/2bc8f917-262d-4ea8-a1c9-b2997ef1f256')
 manifest=json.loads((original/'manifest.json').read_text())
 integrity=[]
 for row in manifest['documents']:
  path=original/row['path'];actual=digest(path.read_bytes()) if path.exists() else None
  integrity.append({'path':row['path'],'expected_sha256':row.get('sha256'),'actual_sha256':actual,'matches':actual==row.get('sha256'),'declared_pages':row.get('pages')})
 out['original_package_integrity']=integrity
 source=next(original/r['path'] for r in manifest['documents'] if r.get('sha256')=='cfd76796ef1d366905dc14d079a2d3a6e3c288f317f4377829a83f7e9c28c5d1')
 assert digest(source.read_bytes())=='cfd76796ef1d366905dc14d079a2d3a6e3c288f317f4377829a83f7e9c28c5d1'
 shutil.copyfile(source,root/'frozen-specification.pdf')
 gold=Path('eval/gold/confirmed-GEM_9671792-items-10-11.csv');shutil.copyfile(gold,root/'frozen-gold.csv')
 out['frozen_sources']={'specification_sha256':digest(source.read_bytes()),'gold_sha256':digest(gold.read_bytes()),'gold_scope':'six confirmed compound requirement/manufacturer pairs for two BOQ items; no whole-tender extraction gold'}
 code=hashlib.sha256()
 for path in sorted(Path('backend/app').rglob('*.py')):
  code.update(str(path).encode());code.update(path.read_bytes())
 out['code_fingerprint']=code.hexdigest()
 config={k:v for k,v in settings.model_dump().items() if k.startswith(('embedding_','match_','comparison_','retrieval_'))}
 out['private_retrieval_config_fingerprint']=digest(json.dumps(config,sort_keys=True,default=str).encode())
 t=time.perf_counter();read=read_specification(source)
 out['local_source_read']={'seconds':round(time.perf_counter()-t,3),'rows':len(read.rows),'pages':len(read.pages),'states':[p.status for p in read.pages],'error_type':read.error.split(':')[0] if read.error else None}
 labels,unlabelled,unconfirmed=load_labels(gold)
 with session_scope() as s:
  s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
  repo=TenderRepo(s);reqs=repo.list_requirements(tid);items={r['item_id']:r for r in repo.list_items(tid)}
  assert len(reqs)==48 and len(items)==11
  keys=('id','text','type','attribute','value','unit','operator','mandatory','page','item_id','source_table_id','source_row_id','review_required','review_reason')
  frozen=[{k:r.get(k) for k in keys} for r in reqs]
  (root/'frozen-requirements.json').write_text(json.dumps({'tender_id':tid,'items':items,'requirements':frozen},indent=2,default=str),encoding='utf-8')
  out['frozen_requirements_sha256']=digest((root/'frozen-requirements.json').read_bytes())
  docs=[{k:str(v) if v is not None else None for k,v in row.items()} for row in s.execute(select(document)).mappings()]
  h=hashlib.sha256()
  for row in sorted(docs,key=lambda r:r['id']):h.update(json.dumps(row,sort_keys=True).encode())
  chunk_count=0
  for row in s.execute(select(chunk.c.id,chunk.c.document_id,chunk.c.page,chunk.c.text,cast(chunk.c.embedding,Text)).order_by(chunk.c.id)):
   h.update(json.dumps(list(row),default=str).encode());chunk_count+=1
  out['corpus_snapshot']={'documents':len(docs),'chunks':chunk_count,'metadata_text_vector_fingerprint':h.hexdigest()}
  (root/'corpus-document-inventory.json').write_text(json.dumps(docs,indent=2),encoding='utf-8')
  from app.services.requirement.extract import PROMPT_VERSION as requirement_prompt
  out['stored_extraction_version_gate']={'current_rows':sum(r.get('prompt_version')==requirement_prompt for r in reqs),'outdated_rows':sum(r.get('prompt_version')!=requirement_prompt for r in reqs),'new_matching_runs_require_refresh':any(r.get('prompt_version')!=requirement_prompt for r in reqs)}
  out['stored_extraction']={'requirements':len(reqs),'items':len(items),'item_linked':sum(bool(r['item_id']) for r in reqs),'with_attribute':sum(bool(r['attribute']) for r in reqs),'review_required':sum(bool(r['review_required']) for r in reqs),'accuracy':'unmeasured; no complete confirmed extraction inventory'}
  save();emit('frozen',requirements=len(reqs),original_package_hash_failures=sum(not r['matches'] for r in integrity))
  t=time.perf_counter()
  with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
   model=_sentence_transformer(settings.embedding_model)
  out['embedding_startup']={'seconds':round(time.perf_counter()-t,3),'device':str(model.device)}
  emit('embedding_ready',**out['embedding_startup']);save()
  class CurrentRanking(DatabaseRanking):
   def result_status(self,label):return None
  out['retrieval']=[]
  for phase in ('first','warm'):
   s.info.clear();s.info['requirement_embeddings']={};begin=time.perf_counter()
   report=evaluate(labels,CurrentRanking(s),k_values=(3,20),unlabelled=unlabelled,unconfirmed=unconfirmed)
   gold_result={'label_count':report.total,'page_hits':{str(k):report.hits(k) for k in (3,20)},'document_hits':{str(k):report.document_hits(k) for k in (3,20)}}
   workload=[]
   for r in reqs:
    item=items.get(r['item_id'],{});category=item.get('category')
    if not category or category=='unknown':continue
    query=build_query(item.get('name'),r['text'],attribute=r['attribute'],value=r['value'],unit=r['unit'])
    kind=infer_type(category,item.get('name') or '').product_type
    for mfr in ('Flowserve','delval_flow','valmatic'):
     candidates=search(s,query,limit=20,manufacturer_name=mfr,category=category,product_type=kind)
     workload.append({'requirement_id':str(r['id']),'item_id':r['item_id'],'manufacturer':mfr,'chunks':[c.chunk_id for c in candidates]})
    if len(workload)%30==0:emit('retrieval_progress',pass_name=phase,pairs=len(workload))
   result={'phase':phase,'seconds':round(time.perf_counter()-begin,3),'gold':gold_result,'pairs':len(workload),'zero_candidate_pairs':sum(not r['chunks'] for r in workload),'ranking_fingerprint':digest(json.dumps(workload,sort_keys=True,default=str).encode())}
   out['retrieval'].append(result);(root/f'rankings-{phase}.json').write_text(json.dumps(workload,indent=2,default=str),encoding='utf-8');save();emit('retrieval_complete',result=result)
  no_remote=NoRemote();begin=time.perf_counter();assessments=[]
  for r in reqs:
   for mfr in ('Flowserve','delval_flow','valmatic'):
    try:
     result,category=_assess(s,r,items,manufacturer_name=mfr,matcher=match_requirement,client=no_remote)
     assessments.append({'requirement_id':str(r['id']),'item_id':r['item_id'],'manufacturer':mfr,'state':'evaluated_without_generation','status':result.status.value,'review_required':result.review_required,'verification_outcome':result.verification_outcome.value,'reason_code':result.reason_code.value if result.reason_code else None})
    except LLMBudgetExceeded:
     assessments.append({'requirement_id':str(r['id']),'item_id':r['item_id'],'manufacturer':mfr,'state':'pending_generation'})
    except Exception as exc:
     assessments.append({'requirement_id':str(r['id']),'manufacturer':mfr,'state':'error','error_type':type(exc).__name__})
     out['assessment_error_type']=type(exc).__name__;break
   if out.get('assessment_error_type'):break
   if len(assessments)%30==0:emit('cached_assessment_progress',pairs=len(assessments))
  from collections import Counter
  out['cached_assessment']={'seconds':round(time.perf_counter()-begin,3),'pairs_attempted':len(assessments),'states':dict(Counter(r['state'] for r in assessments)),'blocked_generation_attempts':no_remote.blocked,'accuracy':'not computed: confirmed compound labels are not interchangeable with atomic requirements'}
  out['page_context_cache']={'lookups':dict(cache_counts),'seconds':round(cache_seconds,3),'source_hash_verified_before_every_hit':True}
  (root/'cached-assessments.json').write_text(json.dumps(assessments,indent=2),encoding='utf-8')
  s.rollback()
 out['total_diagnostic_seconds']=round(time.perf_counter()-started,3);save();emit('finished',**out['cached_assessment'])
except Exception as exc:
 out['error_type']=type(exc).__name__;save();emit('stopped',error_type=type(exc).__name__)
