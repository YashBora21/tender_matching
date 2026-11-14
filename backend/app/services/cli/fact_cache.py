"""Read-only fact-cache recovery and mapping review. Never applies proposals."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import csv
from hashlib import sha256
import json
from pathlib import Path
import re

from sqlalchemy import text
from app.comparison.attributes import attribute_by_term
from app.comparison.vocabulary import load_vocabulary, vocabulary_path
from app.services.requirement.structured_values import normalize_fields, resolve_name
from app.services.match.verify import check_source_anchor, check_range_endpoint, verify
from app.services.textnorm import normalise_text


def digest(value):
    return sha256(json.dumps(value,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


def inventory(rows):
    claims=defaultdict(list);sources=defaultdict(list);products=defaultdict(list)
    for row in rows:
        assertion=(row.get('product_id'),row.get('attribute') or row.get('original_name'),*(row.get(k) for k in ('value','unit','value_type','operator','conditions')))
        claims[assertion].append(row['fact_id'])
        sources[(*assertion,*(row.get(k) for k in ('document_id','page','quote')))].append(row['fact_id'])
        if row.get('attribute') and (row.get('value') or row.get('quote')):
            products[row['product_id'],row['attribute']].append(row)
    conflicts=[]
    for key,facts in products.items():
        variants={tuple(r.get(k) for k in ('value','unit','value_type','operator','conditions')) for r in facts}
        if len(variants)>1:
            conflicts.append({'product_id':key[0],'attribute':key[1],
                'fact_ids':[r['fact_id'] for r in facts],
                'condition_variants':len({r.get('conditions') for r in facts}),
                'decision':'retain_distinct_review_before_cache_selection'})
    return {'facts':len(rows),'empty_nonclaim_rows':sum(not r.get('value') and not r.get('quote') for r in rows),
        'claim_bearing_rows':sum(bool(r.get('value') or r.get('quote')) for r in rows),'by_status':dict(Counter(r['verification_status'] for r in rows)),
        'missing_context':sum(not r.get('source_context') for r in rows),
        'verified_missing_context':sum(r['verification_status']=='verified' and not r.get('source_context') for r in rows),
        'source_pages':len({(r['document_id'],r['page']) for r in rows}),
        'duplicate_claim_groups':[v for v in claims.values() if len(v)>1],
        'duplicate_source_groups':[v for v in sources.values() if len(v)>1],
        'cache_key_conflicts':conflicts}


def _same(left,right):
    return normalise_text(str(left or '')).casefold()==normalise_text(str(right or '')).casefold()


REVIEW_SOURCE_FIELDS = ('fact_id','manufacturer','model','attribute','original_name','value',
    'unit','conditions','page','quote','path','sha256','verification_status')


def load_review(path, rows):
    """Bind reviewer decisions to unchanged source fields; never infer scope approval."""
    current = {row['fact_id']: row for row in rows}
    reviews = {}
    original_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(max(original_limit, path.stat().st_size))
        with path.open(encoding='utf-8-sig', newline='') as source:
            reader = csv.DictReader(source)
            if not set((*REVIEW_SOURCE_FIELDS, 'human_decision', 'human_notes')).issubset(reader.fieldnames or []):
                raise ValueError('review sheet is missing required columns')
            for entry in reader:
                identity = entry['fact_id']
                if identity not in current or identity in reviews:
                    raise ValueError('review contains an unknown or duplicate fact identity')
                row = current[identity]
                if any(entry.get(key) != ('' if row.get(key) is None else str(row[key])) for key in REVIEW_SOURCE_FIELDS):
                    raise ValueError('review source fields are stale or modified; generate a new review')
                decision = entry['human_decision'].strip().lower()
                if decision not in ('supported', 'incorrect', 'uncertain') or not entry['human_notes'].strip():
                    raise ValueError('each reviewed row needs a valid decision and notes')
                reviews[identity] = {'decision': decision, 'notes': entry['human_notes'],
                    'original_fact_sha256': digest(row)}
    finally:
        csv.field_size_limit(original_limit)
    file_hash = sha256(path.read_bytes()).hexdigest()
    return {identity: dict(review, sheet_sha256=file_hash) for identity, review in reviews.items()}


def native_quote_spans(context, quote):
    """Locate the original quote without creating a model/attribute binding."""
    needle = normalise_text(quote or '')
    lines = context.get('native_lines') or []
    joined = ''; offsets = []
    for line in lines:
        value = normalise_text(line.get('text') or '')
        start = len(joined) + bool(joined)
        joined += (' ' if joined else '') + value
        offsets.append((start, len(joined), line))
    matches = []
    if needle:
        for match in re.finditer(re.escape(needle), joined):
            members = [line for start, end, line in offsets if start < match.end() and end > match.start()]
            matches.append({'line_ids': [line['line_id'] for line in members],
                'lines': members, 'normalized_span': [match.start(), match.end()],
                'review_required': True, 'method': 'exact_native_quote_span',
                'reason': 'quote location alone does not establish attribute, model or conditions'})
    return matches


def recover_fact(row,context,page_text,*,scope_reviewed=False,review_blockers=(),aliases=(),human_review=None):
    """Retain the claim;enumerate exact cells and run the unchanged verifier."""
    context=deepcopy(context)
    context.pop('extraction_model',None)
    context.pop('fact_prompt_version',None)
    context['passage']=page_text
    claim={key:row.get(key) for key in ('value','unit','operator','conditions','quote')}
    attribute,_=resolve_name(row.get('attribute') or row.get('original_name'),aliases)
    normalized = normalize_fields(attribute or row.get('original_name'), row.get('value'), row.get('unit'), aliases=aliases)
    claim['attribute']=attribute or row.get('original_name')
    original=(row.get('source_context') or {}).get('claim') or {}
    if original.get('model_or_series'):
        claim['model_or_series']=original['model_or_series']
    context['claim']=claim
    blockers=list(review_blockers)
    if human_review:
        if human_review.get('original_fact_sha256') != digest(row):
            raise ValueError('review is not bound to the current fact snapshot')
        context['human_review'] = deepcopy(human_review)
        if human_review['decision'] != 'supported':
            blockers.append('reviewer_' + human_review['decision'])
    if not row.get('value') and not row.get('quote'):blockers.append('empty_nonclaim_placeholder')
    if row.get('document_product_id') and row['document_product_id']!=row.get('product_id'):
        blockers.append('linked_document_product_mismatch')
    if not attribute or attribute_by_term(attribute) is None:
        blockers.append('canonical_attribute_unresolved')
    if normalized['review_required']:
        blockers.append('normalization:' + normalized['review_reason'])
    spec = attribute_by_term(attribute or '')
    report=verify(quote=row.get('quote'),page_text=page_text,passage=page_text,
        document_manufacturer=row.get('document_manufacturer'),document_model=context.get('document_model'),
        document_family=context.get('document_family'),document_model_or_family=context.get('model_or_family'),
        expected_manufacturer=row.get('manufacturer'),expected_model=row.get('model'),
        is_family_level=bool(row.get('is_family_level') or row.get('document_is_family_level')),attribute=attribute,value=row.get('value'),
        requirement_unit=row.get('unit'),capability_unit=row.get('unit'),
        operator_is_known=bool(row.get('operator')) and row.get('operator')!='unknown',source_tables=context.get('tables',[]),
        convertible=(not normalized['review_required'] if spec and getattr(spec,'normalization',None)=='nominal' else None))
    blockers.extend(report.review_reasons)
    range_gate=check_range_endpoint(row.get('quote'),row.get('value'))
    if range_gate:blockers.append(range_gate.reason)
    choices=[]
    for table_index,table in enumerate(context.get('tables',[])):
        for row_index,cells in enumerate(table[1:],1):
            for column_index,cell in enumerate(cells):
                if not row.get('value') or not _same(cell,row['value']):continue
                candidate=deepcopy(context)
                candidate['claim']=dict(claim,table_index=table_index,row_index=row_index,column_index=column_index)
                gate=check_source_anchor(candidate,attribute,row.get('value'))
                if gate:continue
                scope=[]
                for i,header in enumerate(table[0]):
                    if re.sub(r'[^a-z]','',str(header).casefold()) in ('model','series','modelseries','modelnumber'):
                        if i<len(cells) and _same(cells[i],row.get('model')):scope.append({'header':header,'value':cells[i]})
                source_conditions=[{'header':h,'value':cells[i]} for i,h in enumerate(table[0])
                    if re.sub(r'[^a-z]','',str(h).casefold()) in ('condition','conditions') and i<len(cells) and cells[i]]
                choices.append({'claim':candidate['claim'],'resolved_anchor':candidate.get('resolved_anchor'),
                    'headers':table[0],'row':cells,'explicit_model_scope':scope,
                    'condition_cells':source_conditions})
    if context.get('tables'):
        # Existing coordinates are usable only against the identical source identity.
        old_indices=tuple(original.get(k) for k in ('table_index','row_index','column_index'))
        old_sha=(row.get('source_context') or {}).get('document_sha256')
        pinned=[c for c in choices if tuple(c['claim'].get(k) for k in ('table_index','row_index','column_index'))==old_indices]
        if old_sha==context.get('document_sha256') and len(pinned)==1:choices=pinned
        if len(choices)!=1:
            blockers.append('table_anchor_missing' if not choices else 'table_anchor_ambiguous')
        else:
            chosen=choices[0];context['claim']=chosen['claim']
            if chosen['resolved_anchor']:context['resolved_anchor']=chosen['resolved_anchor']
            if not chosen['explicit_model_scope'] and not scope_reviewed:
                blockers.append('model_scope_requires_review')
            if any(not row.get('conditions') or normalise_text(c['value']) not in normalise_text(row['conditions']) for c in chosen['condition_cells']):
                blockers.append('source_conditions_not_preserved')
    else:
        gate=check_source_anchor(context,attribute,row.get('value'))
        if gate:blockers.append(gate.reason)
    blockers=list(dict.fromkeys(blockers))
    eligible=not blockers and report.passed and not report.review_required
    return {'fact_id':row['fact_id'],'previous_status':row['verification_status'],
        'proposed_attribute':attribute,'proposed_status':'verified' if eligible else 'needs_review','cache_eligible_after_reviewed_apply':eligible,
        'blockers':blockers,'layers':[{'layer':r.layer.value,'passed':r.passed,'borderline':r.borderline,'reason':r.reason} for r in report.layers],
        'source_context':context,'candidate_anchors':choices,'native_quote_spans':native_quote_spans(context,row.get('quote')),
        'normalization':normalized,'human_review':deepcopy(human_review),'original_fact_sha256':digest(row),
        'claim_unchanged':True,'database_writes':0}


def mapping_review(rows,aliases):
    groups=defaultdict(list)
    for row in rows:
        norm=row.get('normalization') or {}
        if row['type'] not in ('technical','certification') or (norm.get('attribute') and not norm.get('review_required')):continue
        groups[row.get('attribute') or ''].append(row)
    vocabulary=load_vocabulary(vocabulary_path())
    output=[]
    for label,records in sorted(groups.items()):
        canonical,reason=resolve_name(label,aliases)
        candidates={canonical} if canonical else set()
        for spec in vocabulary.attributes:
            for term in spec.approved_synonyms:
                pattern=r'(?<!\w)'+re.escape(normalise_text(term).casefold())+r'(?!\w)'
                if any(re.search(pattern,normalise_text((r.get('attribute') or '')+' '+(r.get('value') or '')).casefold()) for r in records):
                    candidates.add(spec.canonical_term)
        output.append({'source_label':label,'observations':len(records),
            'proposed_canonical_attributes':sorted(candidates),
            'proposal_state':'ambiguous' if len(candidates)>1 else 'unapproved_proposal' if candidates else 'unmapped_requires_review',
            'current_reason':reason or 'structured_value_requires_review',
            'approved':False,'reviewed_canonical_attribute':None,'reviewed_component_scope':None,'human_notes':'','component_scope':'unreviewed;see source examples',
            'examples':[{'record_id':str(r['id']),'tender_id':r.get('tender_id'),'item_id':r.get('item_id'),'page':r.get('page'),
                'source_table_id':r.get('source_table_id'),'document_id':r.get('document_id'),'document_sha256':r.get('document_sha256'),
                'source_kind':r.get('source_kind','requirement'),'source_section':r.get('source_section'),'value':r.get('value'),'unit':r.get('unit'),
                'source_text':r.get('source_text'),'table':(r.get('normalization') or {}).get('source_context',{}).get('table')} for r in records]})
    return output


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--review-sheet',type=Path,help='Previously reviewed claims; source fields must still match')
    args=parser.parse_args(argv)
    out=args.output_dir
    if out.exists() and any(out.iterdir()):raise SystemExit('Refusing to overwrite an existing audit/review directory')
    out.mkdir(parents=True,exist_ok=True)
    from app.repositories.db import session_scope
    from app.repositories.aliases import AliasRepo
    from app.services.match.match import _evidence_context,_corpus_file
    from app.services.match.retrieve import Candidate
    import pymupdf
    with session_scope() as session:
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        session.execute(text('SET statement_timeout=30000'))
        rows=[dict(r) for r in session.execute(text("""SELECT f.id::text fact_id,f.product_id::text product_id,
          f.document_id::text document_id,f.attribute_id::text attribute_id,f.original_name,f.value,f.unit,
          f.value_type,f.operator,f.conditions,f.page,f.quote,f.verification_status,f.source_context,f.extractor_version,
          a.canonical_term attribute,p.model,p.family,p.is_family_level,p.category,p.product_type,
          m.name manufacturer,dm.name document_manufacturer,d.path,d.sha256,d.model_or_family,
          d.product_id::text document_product_id,dp.model document_model,dp.family document_family,dp.is_family_level document_is_family_level
          FROM fact f JOIN product p ON p.id=f.product_id JOIN manufacturer m ON m.id=p.manufacturer_id
          JOIN document d ON d.id=f.document_id LEFT JOIN product dp ON dp.id=d.product_id JOIN manufacturer dm ON dm.id=d.manufacturer_id
          LEFT JOIN attribute a ON a.id=f.attribute_id ORDER BY f.id""")).mappings()]
        reviews = load_review(args.review_sheet, rows) if args.review_sheet else {}
        snapshot=digest(rows);before=inventory(rows)
        (out/'inventory.json').write_text(json.dumps(before,indent=2,default=str)+'\n',encoding='utf-8')
        conflict_ids={i for group in before['cache_key_conflicts'] for i in group['fact_ids']}
        human={}
        old_review=Path(__file__).resolve().parents[4]/'eval/audit/evidence-review.csv'
        if old_review.exists():
            with old_review.open(encoding='utf-8-sig',newline='') as f:human={r['fact_id']:r for r in csv.DictReader(f)}
        proposals=[];pages={};aliases=AliasRepo(session).list_all()
        for row in rows:
            proof=human.get(row['fact_id'],{})
            bound=(proof.get('sha256')==row['sha256'] and str(proof.get('page'))==str(row['page'])
                and proof.get('value')==row.get('value') and proof.get('quote')==row.get('quote'))
            reviewed=bound and proof.get('human_source_scope_correct')=='yes'
            blockers=['cache_key_has_distinct_claim_variants'] if row['fact_id'] in conflict_ids else []
            if bound and any(proof.get(k) in ('no','uncertain') for k in ('human_attribute_correct','human_value_correct','human_source_scope_correct')):
                blockers.append('human_review_not_approved')
            try:
                key=(row['document_id'],row['page'])
                if key not in pages:
                    path=_corpus_file(row['path'])
                    if path is None or sha256(path.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('source_integrity_failed')
                    with pymupdf.open(path) as pdf:
                        if not row['page'] or not 1<=row['page']<=len(pdf):raise ValueError('source_page_out_of_bounds')
                        page_text=pdf[row['page']-1].get_text()
                    context=_evidence_context(session,Candidate(chunk_id='',document_id=row['document_id'],text='',page=row['page'],model=row['document_model'],family=row['document_family'],model_or_family=row['model_or_family']))
                    pages[key]=(context,page_text)
                context,page_text=pages[key]
                # Product identity is per fact,even when a page is shared by several products.
                context=dict(context,document_model=row['document_model'],document_family=row['document_family'],model_or_family=row['model_or_family'])
                proposals.append(recover_fact(row,context,page_text,scope_reviewed=reviewed,review_blockers=blockers,aliases=aliases,
                    human_review=reviews.get(row['fact_id'])))
            except (OSError,ValueError,RuntimeError) as exc:
                proposals.append({'fact_id':row['fact_id'],'previous_status':row['verification_status'],
                    'proposed_status':'needs_review','cache_eligible_after_reviewed_apply':False,
                    'blockers':['source_recovery_failed:'+type(exc).__name__],
                    'human_review':reviews.get(row['fact_id']),
                    'source_error_code':str(exc) if str(exc) in ('source_integrity_failed','source_page_out_of_bounds') else type(exc).__name__,
                    'original_fact_sha256':digest(row),'claim_unchanged':True,'database_writes':0})
        reqs=[dict(r) for r in session.execute(text("SELECT id::text,tender_id::text,item_id,source_page AS page,source_section,source_text,source_table_id::text,attribute,value,unit,type,normalization FROM requirement WHERE superseded_at IS NULL AND type IN ('technical','certification') ORDER BY id")).mappings()]
        projected=inventory([dict(r,attribute=p.get('proposed_attribute') or r.get('attribute')) for r,p in zip(rows,proposals)])
        projected_conflicts={i for group in projected['cache_key_conflicts'] for i in group['fact_ids']}
        for p in proposals:
            if p['fact_id'] in projected_conflicts:
                p['proposed_status']='needs_review';p['cache_eligible_after_reviewed_apply']=False
                if 'cache_key_has_distinct_claim_variants' not in p['blockers']:p['blockers'].append('cache_key_has_distinct_claim_variants')
        fact_labels=[{'id':r['fact_id'],'item_id':None,'page':r['page'],'type':'technical','source_kind':'fact',
            'attribute':r.get('original_name') or r.get('attribute'),'value':r.get('value'),'unit':r.get('unit'),
            'document_id':r['document_id'],'document_sha256':r['sha256'],
            'source_section':r.get('model'),'source_text':r.get('quote'),
            'normalization':{'attribute':r.get('attribute'),'review_required':not bool(r.get('attribute'))}}
            for r in rows if r.get('value') or r.get('quote')]
        mappings=mapping_review(reqs+fact_labels,aliases)
        summary={'mode':'dry_run','database_writes':0,'llm_calls':0,'snapshot_sha256':snapshot,
            'before':{k:v for k,v in before.items() if k not in ('duplicate_claim_groups','duplicate_source_groups','cache_key_conflicts')},
            'duplicate_claim_groups':len(before['duplicate_claim_groups']),
            'duplicate_source_groups':len(before['duplicate_source_groups']),
            'cache_key_conflict_groups':len(before['cache_key_conflicts']),
            'projected_by_status':dict(Counter(p['proposed_status'] for p in proposals)),
            'transitions':dict(Counter(p['previous_status']+'->'+p['proposed_status'] for p in proposals)),
            'blockers':dict(Counter(b for p in proposals for b in p['blockers'])),
            'verification_code_sha256':sha256(Path(verify.__code__.co_filename).read_bytes()).hexdigest(),
            'auditor_code_sha256':sha256(Path(__file__).read_bytes()).hexdigest(),
            'recovered_pages':len(pages),'projected_conflict_groups':len(projected['cache_key_conflicts']),
            'proposed_attribute_repairs':sum(bool(p.get('proposed_attribute')) and not r.get('attribute') for r,p in zip(rows,proposals)),
            'mapping_labels':len(mappings),'human_approvals_added':0,'facts_preserved':len(rows),
            'review_decisions':dict(Counter(review['decision'] for review in reviews.values())),
            'claim_normalization_resolved':sum(bool(r.get('value') or r.get('quote')) and not p.get('normalization',{}).get('review_required',True) for r,p in zip(rows,proposals)),
            'native_quote_span_facts':sum(bool(p.get('native_quote_spans')) for p in proposals),
            'eligible_repairs':sum(p['cache_eligible_after_reviewed_apply'] for p in proposals)}
        for name,data in [('facts',proposals),('mapping-review',mappings),('summary',summary)]:
            (out/(name+'.json')).write_text(json.dumps(data,indent=2,default=str)+'\n',encoding='utf-8')
        with (out/'fact-review.csv').open('w',encoding='utf-8',newline='') as f:
            fields=['fact_id','manufacturer','model','attribute','original_name','value','unit','conditions','page','quote','path','sha256','verification_status','proposed_attribute','proposed_status','blockers','candidate_anchors','human_decision','human_notes','normalization','native_quote_spans']
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
            for r,p in zip(rows,proposals):
                data={key:r.get(key) for key in fields};data.update({key:p.get(key) for key in ('proposed_attribute','proposed_status','blockers','candidate_anchors','normalization','native_quote_spans')})
                review=reviews.get(r['fact_id'],{})
                data.update(human_decision=review.get('decision',''),human_notes=review.get('notes',''))
                writer.writerow({key:json.dumps(value) if isinstance(value,(dict,list)) else value for key,value in data.items()})
        with (out/'mapping-review.csv').open('w',encoding='utf-8',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=['source_label','observations','proposed_canonical_attributes','proposal_state','approved','component_scope','current_reason','reviewed_canonical_attribute','reviewed_component_scope','human_notes','examples'])
            writer.writeheader()
            for row in mappings:writer.writerow({k:json.dumps(v) if isinstance(v,(list,dict)) else v for k,v in row.items()})
        print(json.dumps(summary),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
