"""Synthetic document scopes and completion failures; no API or database."""
from app.services.tender.completeness import completion_gaps, page_coverage


def test_equal_page_numbers_in_two_documents_do_not_hide_missing_wrapper_pages():
    tender = {'sha256': 'synthetic-wrapper', 'page_count': 2}
    attachments = [{'id': 'synthetic-spec', 'sha256': 'synthetic-spec-hash', 'pages': 1}]
    pages = [{'page': 1, 'source_key': 'synthetic-spec', 'ledger_status': 'parsed'}]
    assert page_coverage(tender, attachments, pages)['unaccounted'] == 2
    pages += [{'page': n, 'source_key': 'wrapper', 'ledger_status': 'parsed'} for n in (1, 2)]
    coverage = page_coverage(tender, attachments, pages)
    assert coverage['declared'] == coverage['accounted'] == 3
    assert not coverage['incomplete_ingestion']


def test_identical_wrapper_and_attachment_bytes_are_not_double_counted():
    tender = {'sha256': 'synthetic-shared', 'page_count': 1}
    attachments = [{'id': 'source-a', 'sha256': 'synthetic-shared', 'pages': 1},
                   {'id': 'source-b', 'sha256': 'synthetic-shared', 'pages': 1}]
    pages = [{'page': 1, 'source_key': 'source-a', 'ledger_status': 'ocr'}]
    coverage = page_coverage(tender, attachments, pages)
    assert coverage['declared'] == 1 and not coverage['incomplete_ingestion']


def test_nonempty_ocr_garbage_is_not_complete_ingestion():
    clauses = [{'type': 'unknown', 'item_id': None, 'review_reason': 'unreadable synthetic clause'}]
    assert completion_gaps({}, [], [], clauses)


def test_failed_and_unexplained_skipped_pages_are_unresolved():
    pages = [{'page': 1, 'ledger_status': 'failed'}, {'page': 2, 'ledger_status': 'skipped'}]
    assert len(page_coverage({'page_count': 2}, [], pages)['unresolved']) == 2


def test_successful_typed_and_linked_source_is_complete():
    pages = [{'page': 1, 'source_key': 'wrapper', 'ledger_status': 'parsed'}]
    requirements = [{'type': 'technical', 'item_id': '1', 'attribute': 'synthetic_attribute'}]
    assert completion_gaps({'page_count': 1}, [], pages, requirements) == []
