"""Matching a robots.txt the way the site that wrote it means it.

`urllib.robotparser` is the standard answer and it is not enough: it reads
`Disallow: *.pdf` as no rule at all, which is the line a manufacturer writes to
keep its catalogues out of crawlers. These tests are the rules that answer
carries. The text in them is synthetic - it is written here to exercise the
matching, not quoted from any host.
"""

from __future__ import annotations

import pytest

from app.core.robots import allows, deciding_rule

AGENT = "tender-corpus-collector/1.0"


def _verdict(text: str, url: str, agent: str = AGENT) -> tuple[bool, str | None]:
    return allows(url, text, agent)


def test_a_wildcard_rule_bans_the_document_it_names():
    """The rule this exists for: `Disallow: *.pdf` means the PDFs."""
    text = "User-agent: *\nDisallow: *.pdf\n"

    assert not _verdict(text, "https://maker.example/catalogue.pdf")[0]
    # The same rule under a directory: a wildcard is not a path prefix.
    assert not _verdict(text, "https://maker.example/resource/blob/9/data.pdf")[0]
    # And a document it says nothing about is left alone.
    assert _verdict(text, "https://maker.example/index.html")[0]


def test_the_standard_library_parser_does_not_ban_that_document():
    """The gap, pinned: the thing this module replaces does not catch it."""
    import urllib.robotparser

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: *.pdf"])
    assert parser.can_fetch(AGENT, "https://maker.example/catalogue.pdf")


def test_a_leading_slash_wildcard_is_a_path_that_starts_anyway():
    text = "User-agent: *\nDisallow: /*.pdf\n"

    assert not _verdict(text, "https://maker.example/a/b/datasheet.pdf")[0]
    assert _verdict(text, "https://maker.example/a/b/datasheet.html")[0]


def test_an_anchored_rule_stops_at_the_end_it_declares():
    """`$` is the end of the path, so a longer path is not the banned one."""
    text = "User-agent: *\nDisallow: /files/*.pdf$\n"

    assert not _verdict(text, "https://maker.example/files/a.pdf")[0]
    assert _verdict(text, "https://maker.example/files/a.pdf.txt")[0]
    assert _verdict(text, "https://maker.example/other/a.pdf")[0]


def test_an_unanchored_rule_decides_by_prefix():
    """The more careful of the two readings: `/a.pdf` covers `/a.pdf.old`."""
    text = "User-agent: *\nDisallow: /a.pdf\n"

    assert not _verdict(text, "https://maker.example/a.pdf")[0]
    assert not _verdict(text, "https://maker.example/a.pdf.old")[0]


def test_the_longer_allow_carves_its_exception_out():
    text = (
        "User-agent: *\n"
        "Disallow: /private/\n"
        "Allow: /private/public-datasheet.pdf\n"
    )

    assert _verdict(text, "https://maker.example/private/public-datasheet.pdf")[0]
    assert not _verdict(text, "https://maker.example/private/other.pdf")[0]


def test_the_group_that_names_the_agent_is_the_one_that_applies():
    """A ban written for another crawler is not a ban written for this one."""
    text = (
        "User-agent: BadBot\n"
        "Disallow: /\n"
        "\n"
        "User-agent: tender-corpus-collector\n"
        "Disallow: /catalogue/\n"
    )

    assert not _verdict(text, "https://maker.example/catalogue/pump.pdf")[0]
    assert _verdict(text, "https://maker.example/news/press.html")[0]


def test_the_wildcard_group_applies_when_no_group_names_the_agent():
    text = "User-agent: SomeOtherBot\nDisallow: /\n"

    assert _verdict(text, "https://maker.example/a.pdf")[0]


def test_an_empty_disallow_restricts_nothing():
    text = "User-agent: *\nDisallow:\n"

    assert _verdict(text, "https://maker.example/a.pdf")[0]
    assert deciding_rule(text, AGENT, "https://maker.example/a.pdf") is None


def test_a_file_of_no_rules_leaves_the_document_to_its_fetcher():
    text = "Sitemap: https://maker.example/sitemap.xml\nCrawl-delay: 2\n"

    assert _verdict(text, "https://maker.example/a.pdf") == (True, None)


def test_the_refusal_names_the_rule_it_refused():
    """A refusal has to be readable after the fact, so the note says why."""
    text = "User-agent: *\nDisallow: *.pdf\n"

    allowed, note = _verdict(text, "https://maker.example/a.pdf")
    assert not allowed
    assert note == "robots.txt disallows this path for the collector's user agent"


@pytest.mark.parametrize(
    "url",
    [
        "https://maker.example/catalogue.pdf",
        "https://maker.example/deep/path/to/file.pdf",
    ],
)
def test_a_ban_holds_however_deep_the_document_sits(url: str):
    text = "User-agent: *\nDisallow: *.pdf\n"

    assert not _verdict(text, url)[0]
