"""The corpus catalogue: the rules the seeding follows."""

from __future__ import annotations

from app.services.corpus.seed import SeedReport, _family_of, _title_of


def test_a_title_is_read_from_the_file_name():
    assert _title_of("raw/tech_spec_pump_1308.pdf") == "tech_spec_pump_1308"
    assert _title_of(None) is None
    assert _title_of("raw/index.pdf") is None


def test_a_series_is_read_out_of_the_document_title():
    """The title names the series the document covers, and the series is the
    family."""
    assert (
        _family_of("raw/1671729175-Technical-Bulletin-SERIES-75-76.pdf")
        == "Series 75/76"
    )
    assert (
        _family_of("raw/1659634039-Final-Series-51-53-v5b_04-08-2022.pdf")
        == "Series 51/53"
    )
    assert _family_of("raw/1660775902-Final-Series-5C-5D_2022_v3b.pdf") == "Series 5C/5D"


def test_a_year_in_the_title_is_a_date_and_not_a_series():
    """"Final-Series-58-2022" is the series 58 and a date."""
    assert _family_of("raw/1666196897-Final-Series-58-2022-M2.pdf") == "Series 58"


def test_a_title_with_no_series_is_the_family_on_its_own():
    assert _family_of("raw/motors-flyer.pdf") == "motors-flyer"


def test_the_summary_lists_what_a_reviewer_needs_to_see():
    """The summary says what was made, how many are family-level, and which
    documents had no model."""
    report = SeedReport(
        products=5,
        family_level=2,
        family_level_documents=["acme: catalogue A"],
        unnamed_documents=["acme: raw/no-title.pdf"],
        per_manufacturer={"acme": 3, "other": 2},
    )

    summary = report.summary()

    assert "acme" in summary
    assert "family-level products   2" in summary
    assert "catalogue A" in summary
    assert "raw/no-title.pdf" in summary
