"""Measure frozen RC app code on one saved package; no attachment refetching."""
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
import hashlib, json, logging, os, sys, time, uuid
from dotenv import load_dotenv

ROOT = Path.cwd()
snapshot = Path((ROOT / '.audit_tmp/phase3-snapshot.txt').read_text().strip())
load_dotenv(ROOT / '.env', override=True)
os.environ['DATA_DIR'] = str(ROOT / 'backend/data')
os.environ['LLM_CALL_BUDGET'] = os.environ['PHASE3_CALL_BUDGET']
os.environ['LLM_TOKEN_BUDGET'] = os.environ['PHASE3_TOKEN_BUDGET']
os.environ['HF_HUB_OFFLINE'] = os.environ['TRANSFORMERS_OFFLINE'] = '1'
sys.path.insert(0, str(snapshot / 'backend'))
logging.disable(logging.CRITICAL)
from app.core.config import get_settings
from app.repositories.db import session_scope
from app.repositories.schema import llm_spend
from app.repositories.tender import TenderRepo, LLMCache
from app.schemas.enums import TenderStatus
from app.services.llm import LLMClient, LLMBudgetExceeded
from app.services.corpus import audit as audit_module
from app.services.tender import pipeline
from app.services.tender.source_policy import source_context, blocking_requirement
from app.services.tender.upload import effective_limits

settings = get_settings()
tender_id = os.environ['PHASE3_TENDER_ID']
mode = sys.argv[1]
outdir = ROOT / '.audit_tmp/phase3-measurement'
outdir.mkdir(exist_ok=True)
journal = outdir / 'calls.jsonl'
tariff = json.loads((ROOT / '.audit_tmp/phase3-tariff.json').read_text())
conversion = Decimal(os.environ['PHASE3_USD_INR_BUDGET_RATE'])
price_in = Decimal(str(tariff['usd_input_per_million'])) * conversion / 1_000_000
price_out = Decimal(str(tariff['usd_output_per_million'])) * conversion / 1_000_000
spend_cap = Decimal(os.environ['PHASE3_MAX_ESTIMATED_INR'])
totals = defaultdict(float)

def append(entry):
    with journal.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry, default=str) + '\n')
        handle.flush()
        os.fsync(handle.fileno())

def spent():
    calls = {}
    if journal.exists():
        for line in journal.read_text(encoding='utf-8').splitlines():
            entry = json.loads(line)
            calls[entry['id']] = Decimal(str(entry.get('charged_bound_inr', entry['reserved_inr'])))
    return sum(calls.values(), Decimal(0))

class MeasuredClient(LLMClient):
    def _chat_once(self, system, user, *, max_tokens=None):
        output_bound = max_tokens or settings.llm_max_tokens
        # UTF-8 bytes plus framing deliberately overestimate prompt tokens.
        input_bound = len(system.encode('utf-8')) + len(user.encode('utf-8')) + 512
        reserved = input_bound * price_in + output_bound * price_out
        if spent() + reserved > spend_cap:
            raise LLMBudgetExceeded('the phase 3 estimated INR reservation cap would be exceeded')
        entry = {'id':str(uuid.uuid4()), 'mode':mode, 'event':'reserved',
                 'input_token_bound':input_bound, 'output_token_bound':output_bound,
                 'reserved_inr':float(reserved)}
        append(entry)
        begin = time.perf_counter()
        try:
            result = super()._chat_once(system, user, max_tokens=max_tokens)
        except Exception as exc:
            entry.update(event='uncertain', error_type=type(exc).__name__, seconds=time.perf_counter()-begin,
                         charged_bound_inr=float(reserved))
            append(entry)
            raise
        seconds = time.perf_counter() - begin
        cost = result.prompt_tokens * price_in + result.completion_tokens * price_out
        response = result.content
        for private in (self.api_key, self.model, self.base_url):
            if private: response = response.replace(private, '[redacted]')
        with (outdir / 'responses.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'id':entry['id'],'mode':mode,'call':self.calls,
                'source_hash':hashlib.sha256(user.encode()).hexdigest(),'response':response})+'\n')
        # Record this benchmark's successful calls; older spend remains unknown.
        with session_scope() as ledger_session:
            ledger_session.execute(llm_spend.insert().values(id=uuid.UUID(entry['id']),
                model=self.model, purpose='phase3_read', prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens, cost_inr=cost))
        entry.update(event='answered', prompt_tokens=result.prompt_tokens,
                     completion_tokens=result.completion_tokens, seconds=seconds,
                     charged_bound_inr=float(cost))
        append(entry)
        print(json.dumps({'event':'call', 'mode':mode, 'call':self.calls,
                          'seconds':round(seconds,3), 'tokens':result.prompt_tokens+result.completion_tokens,
                          'combined_estimated_inr':float(spent())}), flush=True)
        return result

for name in ['_read_items', '_read_package', '_extract_all', '_persist']:
    original = getattr(pipeline, name)
    def measured(*args, _fn=original, _name=name, **kwargs):
        begin = time.perf_counter()
        try: return _fn(*args, **kwargs)
        finally: totals[_name] += time.perf_counter() - begin
    setattr(pipeline, name, measured)

directory = settings.data_dir / 'tenders' / tender_id
manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
sources = [dict(path=manifest['filename'], sha256=manifest['wrapper_sha256'])] + manifest['documents']
for source in sources:
    if not source.get('path'): continue
    actual = hashlib.sha256((directory / source['path']).read_bytes()).hexdigest()
    assert actual == source['sha256'], 'Frozen source hash differs; no model call allowed'

begin = time.perf_counter()
client = MeasuredClient()
with session_scope() as session:
    repo = TenderRepo(session)
    tender = repo.get_tender(tender_id, for_update=True)
    assert tender['status'] != 'processing', 'Another read is active'
    limits = effective_limits()
    limits.update(phase3_estimated_inr_cap=float(spend_cap), phase3_usd_inr_budget_rate=float(conversion),
                  phase3_input_per_million_inr=float(price_in*1_000_000),
                  phase3_output_per_million_inr=float(price_out*1_000_000))
    limits['phase3_driver_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    attempt = repo.start_reading_attempt(tender_id, limits=limits,
        sha256=tender['sha256'], filename=tender['filename'])
    repo.set_status(tender_id,TenderStatus.PROCESSING)
    session.commit()
    audit = audit_module.audit_tender(directory, repo=repo,
        cache=LLMCache(session,tender_id=tender_id,reading_id=attempt), client=client, force=mode=='bounded')
    status = (TenderStatus.FAILED if audit.failure else TenderStatus.RATE_LIMITED if audit.rate_limited else
              TenderStatus.INCOMPLETE if audit.completion_gaps or not (audit.pages_read and audit.items and audit.requirements)
              else TenderStatus.DONE)
    stop = audit.failure or audit.rate_limited_detail or '; '.join(audit.completion_gaps) or None
    rows = repo.list_requirements(tender_id)
    pages = repo.list_pages(tender_id)
    metrics = {k:getattr(audit,k) for k in ('llm_calls','llm_cache_hits','prompt_tokens','completion_tokens',
        'llm_retries','requirements','pages_read','items','validation_attempts')}
    metrics.update(wall_seconds=time.perf_counter()-begin,stage_seconds=dict(totals),
                   combined_estimated_inr=float(spent()), rc_commit=snapshot.name.removeprefix('phase3-'))
    repo.finish_reading_attempt(attempt,status=status.value,stop_reason=stop,metrics=metrics)
    repo.set_status(tender_id,status,error=stop)
    session.commit()
    counts = Counter((source_context(row).get('source_role','legacy'),row['type'],
                      'fragment' if source_context(row).get('fragment_reason') else
                      'context' if source_context(row).get('context_only') else 'candidate') for row in rows)
    blockers = [row for row in rows if blocking_requirement(row) and (
        row.get('unreadable') or row.get('type')=='unknown' or not row.get('item_id') or
        ('source_coverage_gap' in (row.get('review_reason') or '')) or
        (row.get('type')=='technical' and not row.get('attribute')))]
    output = {'tender_id':tender_id,'attempt_id':attempt,'mode':mode,'status':status.value,'stop_reason':stop,
              'metrics':metrics,'limits':limits,'stages':audit.stages,
              'source_counts':[dict(role=k[0],type=k[1],classification=k[2],count=v) for k,v in sorted(counts.items())],
              'page_states':dict(Counter(p['ledger_status'] for p in pages)),
              'wrapper_pages':sum(p.get('source_key')=='wrapper' for p in pages),
              'blockers':[{k:r.get(k) for k in ('id','text','source_text','type','page','item_id','attribute','value',
                                              'unit','review_reason','normalization')} for r in blockers],
              'label_grade':'No whole-tender extraction gold; no manufacturer assessments made',
              'collection':'Frozen saved package; network collection excluded',
              'budget_coverage':'This benchmark only; earlier monthly spend unknown'}
    output['page_ledger'] = [{'source_key':k[0],'state':k[1],'count':v} for k,v in Counter(
        (p.get('source_key') or str(p.get('attachment_id')),p['ledger_status']) for p in pages).items()]
    (outdir/(mode+'.json')).write_text(json.dumps(output,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps({'event':'finished','mode':mode,'status':status.value,'attempt_id':attempt,
                      'wall_seconds':metrics['wall_seconds'],'calls':client.calls,
                      'input_tokens':client.prompt_tokens,'output_tokens':client.completion_tokens,
                      'blockers':len(blockers),'combined_estimated_inr':float(spent())}),flush=True)
