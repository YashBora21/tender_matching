"""Page triage tests: keyword scoring, no model (FR-T1).

Every page below is synthetic. None of it is taken from a real tender.
"""

from __future__ import annotations

import pytest

from app.services.pageclassify import (
    BOILERPLATE,
    COMMERCIAL_LEGAL,
    MIXED,
    SAFETY_HSE,
    TECHNICAL,
    classify_page,
    is_non_technical,
)

# Synthetic: a safety annex, written to look like one.
SAFETY_PAGE = """
SAFETY NORMS FOR CONTRACTORS

2. The contractor shall deploy a qualified safety officer for the entire
duration of the work. Personal protective equipment is mandatory for every
worker inside the yard: helmet, safety harness and safety shoes.

3. Scaffolding shall be erected by trained personnel and inspected daily.
Hot work requires a permit to work issued by the safety supervisor, and a
fire extinguisher shall be kept at the site. Any accident or injury shall be
reported immediately; unsafe conditions must be reported before work starts.
"""

# Synthetic: an integrity pact page.
INTEGRITY_PAGE = """
INTEGRITY PACT

The bidder shall not offer any inducement to any employee of the purchaser.
Any breach of this agreement shall be referred to arbitration, and the courts
at the purchaser's headquarters shall have exclusive jurisdiction.

The earnest money deposit shall be forfeited if the bidder withdraws. Liquidated
damages and penalty shall be levied for delay. Payment terms are 30 days from
invoice, and GST shall be charged extra. A performance bank guarantee is
required before the purchase order is issued.
"""

# Synthetic: a specification table page.
SPEC_PAGE = """
TECHNICAL SPECIFICATION - SUBMERSIBLE PUMP

Body material: cast iron. Impeller material: stainless steel.
Pressure rating: Class 150. Size: 100 mm. Flow: 50 m3/h.
Head: 30 m. Efficiency: 85 %. Speed: 1450 rpm.
Temperature: 45 degree C. Voltage: 415 V. Frequency: 50 Hz.
Test: hydro test at 1.5 times the rated pressure.
"""

# Synthetic: a page that carries both a specification and a legal clause.
MIXED_PAGE = """
The pump shall be tested at a pressure of 10 bar and the impeller material
shall be stainless steel.

The bidder shall submit an integrity pact and a performance bank guarantee
with the offer, failing which the bid shall be rejected.
"""

BOILERPLATE_PAGE = """
Table of Contents

1. Preface ................ 1
2. Scope .................. 2

For office use only. Authorised signatory: ____________________
Signature of the bidder with seal.
"""


def test_a_safety_annex_page_is_safety_hse() -> None:
    result = classify_page(SAFETY_PAGE)
    assert result.kind == SAFETY_HSE
    assert is_non_technical(result.kind)
    assert "safety officer" in result.matched[SAFETY_HSE]


def test_an_integrity_pact_page_is_commercial_legal() -> None:
    result = classify_page(INTEGRITY_PAGE)
    assert result.kind == COMMERCIAL_LEGAL
    assert is_non_technical(result.kind)
    assert "integrity pact" in result.matched[COMMERCIAL_LEGAL]


def test_a_specification_table_page_is_technical() -> None:
    result = classify_page(SPEC_PAGE)
    assert result.kind == TECHNICAL
    assert not is_non_technical(result.kind)
    assert "pressure" in result.matched[TECHNICAL]


def test_a_page_with_both_is_mixed_and_still_read() -> None:
    result = classify_page(MIXED_PAGE)
    assert result.kind == MIXED
    assert not is_non_technical(result.kind)


def test_a_boilerplate_page_is_boilerplate() -> None:
    result = classify_page(BOILERPLATE_PAGE)
    assert result.kind == BOILERPLATE
    assert is_non_technical(result.kind)


def test_a_page_of_bare_values_is_technical() -> None:
    # No keyword at all: only values. A spec table can look like this.
    result = classify_page("100 mm | 150 | 45 degree C | 50 m3/h")
    assert result.kind == TECHNICAL


def test_an_empty_page_is_boilerplate() -> None:
    assert classify_page("").kind == BOILERPLATE
    assert classify_page("   \n \n").kind == BOILERPLATE


def test_a_single_incidental_word_does_not_make_a_legal_page_technical() -> None:
    page = (
        "The bidder shall submit an integrity pact and a bank guarantee. "
        "The specification is enclosed as an annexure. Arbitration shall apply."
    )
    assert classify_page(page).kind == COMMERCIAL_LEGAL


@pytest.mark.parametrize(
    "kind, expected",
    [
        (TECHNICAL, False),
        (MIXED, False),
        (COMMERCIAL_LEGAL, True),
        (SAFETY_HSE, True),
        (BOILERPLATE, True),
    ],
)
def test_only_the_non_technical_kinds_skip_the_model(kind: str, expected: bool) -> None:
    assert is_non_technical(kind) is expected
