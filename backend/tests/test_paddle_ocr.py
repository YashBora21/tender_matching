"""Synthetic source geometry and local cache boundaries; no Paddle dependency."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services.tender import ocr
from app.services.tender.ocr_layout import page_layout
from app.core.config import get_settings


def box(text, x, y):
    polygon = [[x, y], [x + 20, y], [x + 20, y + 10], [x, y + 10]]
    return {'text': text, 'score': 0.99, 'polygon': polygon, 'source_polygon': polygon}


def test_geometry_keeps_columns_and_duplicate_rows():
    reading = {'boxes': [box('Synthetic heading', 0, 0), box('Row', 0, 20), box('Part No', 100, 20),
                         box('Nomenclature', 200, 20), box('1', 0, 40), box('X-01', 100, 40),
                         box('Synthetic A', 200, 40), box('2', 0, 60), box('X-01', 100, 60),
                         box('Synthetic B', 200, 60)]}
    layout = page_layout(reading, get_settings())
    assert layout['headers'] == ['Row', 'Part No', 'Nomenclature']
    assert [row['cells'] for row in layout['rows']] == [['1', 'X-01', 'Synthetic A'], ['2', 'X-01', 'Synthetic B']]
    assert layout['rows'][0]['box_indices'][1] != layout['rows'][1]['box_indices'][1]


def test_pixel_cache_does_not_reuse_document_coordinates(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    class FakeWorker:
        ready = {'fingerprint': 'synthetic-v1', 'setup_seconds': 0}
        calls = 0
        def request(self, request):
            self.calls += 1
            return {'boxes': [box('Synthetic source', 10, 10)], 'input_size': [100, 100]}
    worker = FakeWorker()
    monkeypatch.setattr(ocr, '_get_worker', lambda: worker)
    class Page:
        rect = SimpleNamespace(width=200, height=300)
        derotation_matrix = SimpleNamespace(a=1, b=0, c=0, d=1, e=0, f=0)
        rotation = 0
        number = 0
        def get_pixmap(self, **kwargs):
            return SimpleNamespace(tobytes=lambda format: b'synthetic-image')
    page = Page()
    first = ocr.read_paddle_page(page)
    page.rect = SimpleNamespace(width=100, height=100)
    second = ocr.read_paddle_page(page)
    assert worker.calls == 1 and second['cache_hit']
    assert first['boxes'][0]['pdf_polygon'][0] == [20, 30]
    assert second['boxes'][0]['pdf_polygon'][0] == [10, 10]
    worker.ready['fingerprint'] = 'synthetic-v2'
    ocr.read_paddle_page(page)
    assert worker.calls == 2


def test_invalid_geometry_cannot_enter_cache():
    reading = {'boxes': [box('Synthetic', 0, 0)], 'input_size': [100, 100]}
    reading['boxes'][0]['source_polygon'][0][0] = float('nan')
    with pytest.raises(ValueError):
        ocr.validate_reading(reading)
