"""Synthetic numeric capability ranges preserve size and units."""
import pytest
from app.comparison.registry import ComparisonRequest, evaluate_numeric
from app.schemas.enums import Operator

@pytest.mark.parametrize('value,unit,status', [('12','inch','full_match'),('304.8','mm','full_match'),('20','inch','missing_capability'),('12','bar','no_evidence_found')])
def test_point_requirement_against_complete_range(value,unit,status):
    request=ComparisonRequest(requirement_value=value,requirement_operator=Operator.EQ,
      requirement_unit=unit,requirement_text_value=value,requirement_range=None,
      capability_value='10-16',capability_unit='inch',capability_text_value='10-16',capability_range=(10,16))
    assert evaluate_numeric(request).status.value==status


def test_cached_range_with_unestablished_conditions_requires_review():
    from app.services.match.match import _from_cached_fact
    cached={'page':1,'document_id':'synthetic','quote':'Synthetic size 10-16 at 20 C',
            'value':'10-16','attribute':'nominal_size','unit':'inch','is_family_level':False,
            'conditions':'at 20 C'}
    result=_from_cached_fact(cached,'Size 12 inches',requirement_value='12',
        requirement_unit='inch',requirement_operator='eq',manufacturer_name='Synthetic',model='Synthetic')
    assert result.status.value=='no_evidence_found' and result.review_required
