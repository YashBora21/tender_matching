"""Synthetic rendered pages: no real tender text or provider calls."""
import pymupdf
from app.services.tender import parse, ingest


def test_only_fully_white_textless_pages_are_blank():
    with pymupdf.open() as doc:
        page = doc.new_page()
        assert parse.is_blank_page(page)
        page.draw_rect(pymupdf.Rect(50, 50, 100, 100), color=None, fill=(0.99, 0.99, 0.99))
        assert not page.get_text() and not parse.is_blank_page(page)
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 60), 'Synthetic source text', color=(1, 1, 1))
        assert not parse.is_blank_page(page)


def test_blank_page_is_accounted_as_skipped_without_ocr(tmp_path, monkeypatch):
    path = tmp_path / 'synthetic-blank.pdf'
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(path)
    def unexpected(*args):
        raise AssertionError('Blank pages must not initialize OCR')
    monkeypatch.setattr(ingest, '_ocr', unexpected)
    monkeypatch.setattr(ingest, '_paddle_layout', unexpected)
    result = ingest.read_specification(path)
    assert len(result.pages) == 1 and result.rows == []
    assert result.pages[0].status == 'skipped'
    assert result.pages[0].skip_reason.startswith('rendered_blank_page:')
    monkeypatch.setattr(parse, 'ocr_page', unexpected)
    corpus = parse._parse_pdf(path)
    assert corpus.page_count == 1 and corpus.failed_pages == []


def test_nonblank_ocr_failure_stays_failed(tmp_path, monkeypatch):
    monkeypatch.setenv('OCR_BACKEND', 'tesseract')
    from app.core.config import get_settings
    get_settings.cache_clear()
    path = tmp_path / 'synthetic-image.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(50, 50, 100, 100), fill=(0, 0, 0))
        doc.save(path)
    monkeypatch.setattr(ingest, '_ocr', lambda *args: ('', 'synthetic OCR failure'))
    result = ingest.read_specification(path)
    assert result.pages[0].status == 'failed'
    assert result.pages[0].error == 'synthetic OCR failure'
