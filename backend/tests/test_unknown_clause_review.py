"""Synthetic unresolved clause; no provider or database access."""
from app.services.output.runs import _assess


def test_unknown_clause_retains_source_review_note_in_manufacturer_outcome():
    row={'text':'Synthetic mixed source clause requiring review','type':'unknown','item_id':None,
         'review_reason':'Synthetic proposed type remains unverified'}
    def forbidden(*args,**kwargs):
        raise AssertionError('unresolved clause must not reach retrieval')
    result,_=_assess(None,row,{},manufacturer_name='Synthetic manufacturer',matcher=forbidden,client=None)
    assert result.review_required
    assert row['review_reason'] in result.review_reason
    assert result.requirement_text==row['text']
