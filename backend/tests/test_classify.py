"""The corpus classifier: the rules, without the database.

Every document named here is synthetic. None of it is a real corpus file.
"""

from __future__ import annotations

from pathlib import Path

from app.services.corpus.classify import (
    ClassifyReport,
    doc_role,
    document_type_of,
    export_unresolved,
)


def test_a_type_is_read_from_the_words_of_the_document() -> None:
    """The document is about a gate valve, so it says so in its own name."""
    assert document_type_of("valves", "gate-valve-datasheet.pdf") == "gate"


def test_a_product_with_no_category_is_not_placed_by_a_stray_word() -> None:
    """A manifest that left the category blank leaves the type blank too. The
    words that would have to supply the category are the same words the type is
    read from, so a bulletin that only mentions a valve would type a whole
    product (D-149)."""
    assert document_type_of(None, "gate-valve-datasheet.pdf") is None
    assert document_type_of("unknown", "gate-valve-datasheet.pdf") is None
    assert document_type_of("valves", "spares-list.pdf") is None


def test_a_type_named_two_ways_equally_is_not_guessed() -> None:
    """A bulletin that names a gate valve and a ball valve equally is not proof
    of either (FR-T9)."""
    assert (
        document_type_of(
            "valves", "gate-valve-and-ball-valve-selection-guide.pdf"
        )
        is None
    )


def test_the_role_the_manifest_declares_wins() -> None:
    """The collector's own word for the document is the first answer, mapped to
    the role vocabulary."""
    assert doc_role("compliance_schedule") == "certificate"
    assert doc_role("specification") == "datasheet"
    assert doc_role("other", "submersible-pump-catalogue.pdf") == "catalogue"


def test_a_non_product_document_is_named_from_its_own_words() -> None:
    """A certificate and a privacy policy are in the corpus as if they were
    products. Their role is what takes them out of the evidence."""
    assert doc_role("other", "ISO-9001-Certificate-Audit-2024.pdf") == "certificate"
    assert doc_role("other", "Company-Privacy-Policy.pdf") == "policy"


def test_a_document_that_names_no_role_is_left_unresolved() -> None:
    assert doc_role(None, "scan-0001.pdf", "") is None


def test_two_roles_named_equally_is_not_a_guess() -> None:
    assert doc_role(None, "safety-datasheet-and-service-catalogue.pdf") is None


def test_the_broken_vocabulary_file_gives_no_roles(tmp_path) -> None:
    """A data file that cannot be read is not an error: nothing is classified,
    and nothing is guessed in its place."""
    missing = tmp_path / "doc_roles.json"
    assert doc_role(None, "gate-valve-datasheet.pdf", path=str(missing)) is None


def test_the_reviewer_gets_one_row_per_undecided_document(tmp_path) -> None:
    """The CSV is the handover: the manufacturer, the product, the document and
    the words the rules saw, with the answer column left blank to fill."""
    report = ClassifyReport(
        documents=3,
        roles=1,
        unresolved=[
            {
                "manufacturer": "acme",
                "product": "Series 9",
                "document": "scan-0001.pdf",
                "product_type": "",
                "doc_role": "",
                "first_page_snippet": "   a   page    with  spaces  ",
            }
        ],
    )

    written = export_unresolved(report, tmp_path / "todo.csv")

    assert written == 1
    lines = (tmp_path / "todo.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "manufacturer,product,document,product_type,doc_role,first_page_snippet"
    assert lines[1].startswith("acme,Series 9,scan-0001.pdf,,,")
