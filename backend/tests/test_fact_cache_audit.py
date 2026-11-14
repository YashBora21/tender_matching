"""Synthetic cache recovery claims;no provider or application database writes."""
from copy import deepcopy
import pytest
from app.services.cli.fact_cache import inventory,recover_fact,mapping_review


def source():
    quote='Synthetic A size 2 inch'
    row=dict(fact_id='synthetic-fact',product_id='synthetic-product',document_id='synthetic-doc',
        manufacturer='Synthetic Manufacturer',document_manufacturer='Synthetic Manufacturer',
        model='Synthetic A',family=None,model_or_family='Synthetic A',is_family_level=False,
        attribute='nominal_size',original_name='size',value='2',unit='inch',value_type='numeric',
        operator='eq',conditions=None,page=1,quote=quote,verification_status='needs_review',source_context=None)
    context={'document_model':'Synthetic A','document_sha256':'a'*64,
             'tables':[[['Model','Size'],['Synthetic A','2']]],'native_lines':[]}
    return row,context,quote


def test_unique_bound_model_cell_is_only_a_proposal_and_does_not_mutate_claim():
    row,context,page=source();before=deepcopy((row,context))
    result=recover_fact(row,context,page)
    assert result['proposed_status']=='verified' and result['cache_eligible_after_reviewed_apply']
    assert result['database_writes']==0 and result['claim_unchanged']
    assert (row,context)==before
    assert result['source_context']['claim']['column_index']==1


@pytest.mark.parametrize('damage',['wrong_column','wrong_model','ambiguous','missing_conditions','range_endpoint','family_scope','bad_quote','unmapped','conflict','human_rejection'])
def test_uncertain_or_wrong_evidence_is_never_a_reusable_repair(damage):
    row,context,page=source()
    blockers=[]
    if damage=='wrong_column':context['tables']=[[['Model','Size','Material'],['Synthetic A','3','2']]]
    if damage=='wrong_model':context['tables'][0][1][0]='Synthetic B'
    if damage=='ambiguous':context['tables'][0].append(['Synthetic A','2'])
    if damage=='missing_conditions':context['tables']=[[['Model','Size','Conditions'],['Synthetic A','2','Synthetic condition']]]
    if damage=='range_endpoint':row.update(value='10',quote='Synthetic A size 10–16 inch');page=row['quote'];context['tables'][0][1][1]='10–16'
    if damage=='family_scope':row['is_family_level']=True
    if damage=='bad_quote':page='Synthetic unrelated text'
    if damage=='unmapped':row['attribute']=None;row['original_name']='Synthetic unknown property'
    if damage=='conflict':blockers=['cache_key_has_distinct_claim_variants']
    if damage=='human_rejection':blockers=['human_review_not_approved']
    result=recover_fact(row,context,page,review_blockers=blockers)
    assert result['proposed_status']=='needs_review' and result['blockers']


def test_missing_local_model_scope_is_not_invented_from_metadata():
    row,context,page=source();context['tables']=[[['Size'],['2']]]
    result=recover_fact(row,context,page)
    assert 'model_scope_requires_review' in result['blockers']
    assert 'model_or_series' not in result['source_context']['claim']


def test_duplicates_and_conflicts_keep_every_fact_id():
    row,_,_=source();duplicate=dict(row,fact_id='synthetic-duplicate',original_name='nominal size')
    conflict=dict(row,fact_id='synthetic-conflict',value='3',conditions='Synthetic different condition')
    result=inventory([row,duplicate,conflict])
    assert result['facts']==3 and result['duplicate_claim_groups']==[['synthetic-fact','synthetic-duplicate']]
    group=result['cache_key_conflicts'][0]
    assert len(group['fact_ids'])==3 and group['condition_variants']==2


def test_mapping_suggestions_are_unapproved_and_retain_scope_examples():
    rows=[dict(id='synthetic-row',type='technical',attribute='Synthetic property',value='flanged ends',
               item_id='synthetic-item',source_section='Synthetic scope',source_text='Synthetic property flanged ends',normalization={})]
    result=mapping_review(rows,[])[0]
    assert not result['approved'] and result['component_scope'].startswith('unreviewed')
    assert result['examples'][0]['item_id']=='synthetic-item'
    assert result['source_label']=='Synthetic property'


def test_existing_audit_directory_is_never_overwritten(tmp_path):
    from app.services.cli.fact_cache import main
    (tmp_path/'human.csv').write_text('Synthetic existing review')
    with pytest.raises(SystemExit,match='overwrite'):
        main(['--output-dir',str(tmp_path)])



def test_empty_placeholder_is_not_a_capability_or_a_conflict():
    row,_,_=source()
    empty=dict(row,fact_id='synthetic-empty',value=None,quote=None)
    result=inventory([row,empty])
    assert result['empty_nonclaim_rows']==1 and result['claim_bearing_rows']==1
    assert result['cache_key_conflicts']==[]


def test_document_scope_cannot_be_replaced_by_fact_product_metadata():
    row,context,page=source();context['document_model']='Synthetic B'
    row['document_product_id']='synthetic-other-product'
    result=recover_fact(row,context,page)
    assert result['proposed_status']=='needs_review'
    assert 'linked_document_product_mismatch' in result['blockers']


def test_approved_label_can_propose_canonical_repair_without_rewriting_old_fact():
    row,context,page=source();row['attribute']=None
    result=recover_fact(row,context,page)
    assert result['proposed_attribute']=='nominal_size' and result['proposed_status']=='verified'
    assert row['attribute'] is None and result['database_writes']==0



def test_database_attribute_cannot_bypass_mapping_approval(monkeypatch):
    from app.services.cli import fact_cache
    row,context,page=source()
    monkeypatch.setattr(fact_cache,'resolve_name',lambda *a,**k:(None,'unapproved attribute mapping'))
    result=fact_cache.recover_fact(row,context,page)
    assert not result['cache_eligible_after_reviewed_apply']
    assert 'canonical_attribute_unresolved' in result['blockers']
