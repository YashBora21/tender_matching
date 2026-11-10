"""Synthetic evidence: range endpoints cannot stand in for complete ranges."""
import pytest
from app.services.match.verify import check_range_endpoint

@pytest.mark.parametrize('quote', ['Sizes 10” - 16” Class 150', 'Size 10-16 inches', 'Size 10 to 16 inches'])
def test_scalar_endpoint_is_rejected(quote):
    assert not check_range_endpoint(quote,'10').passed

def test_complete_range_is_preserved():
    assert check_range_endpoint('Size 10-16 inches','10-16') is None

def test_unrelated_scalar_is_not_rejected():
    assert check_range_endpoint('Size 10-16 inches, Class 150','150') is None
