"""Synthetic source text only; no tender-specific value or offset exceptions."""
import pytest
from app.services.requirement.evidence import span_supports


@pytest.mark.parametrize('source,value', [
    ('Synthetic material CAST  carbon steel', 'cast carbon steel'),
    ('Synthetic size 25.0 mm', '25'),
    ('Synthetic temperature -25 C', '-25'),
    ('Synthetic sizes 12–18 inches', '12 to 18'),
    ('Synthetic sizes 12 - 18 inches', 'between 12 and 18'),
    ('Synthetic sizes between 12 and 18 inches', '12–18'),
    ('Synthetic designation ＺＥＴＡ', 'zeta'),
])
def test_complete_supported_values(source, value):
    assert span_supports(source, (0, len(source)), value)


@pytest.mark.parametrize('source,value', [
    ('Synthetic size 125 mm', '25'),
    ('Synthetic size 25.5 mm', '25'),
    ('Synthetic temperature -25 C', '25'),
    ('Synthetic sizes 12–18 inches', '12'),
    ('Synthetic sizes 12–18 inches', '18'),
    ('Synthetic sizes 12–18 inches', '12–19'),
    ('Synthetic size 12 mm', '12–18'),
    ('Synthetic classes 12 and 18', '12–18'),
    ('Synthetic material CAST carbon stee', 'cast carbon steel'),
    ('Synthetic material alpha', 'beta'),
])
def test_unsupported_or_truncated_values(source, value):
    assert not span_supports(source, (0, len(source)), value)


def test_value_elsewhere_in_clause_does_not_support_wrong_span():
    text = 'Synthetic size 25 mm and pressure 80 bar'
    start = text.index('pressure')
    assert not span_supports(text, (start, len(text)), '25')


def test_clipped_range_endpoint_and_clipped_token_are_rejected():
    text = 'Synthetic sizes 12–18 inches'
    start = text.index('12')
    assert not span_supports(text, (start, start + 2), '12')
    text = 'Synthetic size 125 mm'
    start = text.index('25')
    assert not span_supports(text, (start, start + 2), '25')


def test_clipped_decimal_sign_and_range_limit_cannot_change_the_value():
    for text, fragment, value in [
        ('Synthetic size 25.5 mm', '25', '25'),
        ('Synthetic temperature -25 C', '25', '25'),
        ('Synthetic sizes 12–18.5 mm', '12–18', '12–18'),
    ]:
        start = text.index(fragment)
        assert not span_supports(text, (start, start + len(fragment)), value)


@pytest.mark.parametrize('span', [(-1, 2), (1, 100), (2, 2), (True, 3), (None, 2)])
def test_invalid_offsets(span):
    assert not span_supports('Synthetic text', span, 'text')


def test_nonvalue_clause_still_requires_nonempty_valid_span():
    assert span_supports('Synthetic information', (0, 21))
    assert not span_supports('   ', (0, 3))
