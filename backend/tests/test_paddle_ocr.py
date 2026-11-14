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


def test_signature_only_never_imports_gpu_engine_and_tracks_weights(tmp_path, monkeypatch, capsys):
    import builtins
    import io
    import json
    import sys
    from app.services.tender import paddle_worker
    config = {'extra_sites': [], 'dll_dir': '', 'weights_dir': str(tmp_path),
              'device': 'synthetic-device', 'lang': 'synthetic-language'}
    weight = tmp_path / 'synthetic.pdiparams'
    weight.write_bytes(b'synthetic-weights-A')
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        assert name not in {'cv2', 'numpy', 'paddle', 'paddleocr'}, 'GPU engine imported during cache key construction'
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(sys, 'argv', ['synthetic-worker', '--signature-only'])
    def signature():
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(config)+'\n'))
        paddle_worker.main()
        return json.loads(capsys.readouterr().out)['fingerprint']
    first = signature()
    assert signature() == first
    weight.write_bytes(b'synthetic-weights-B')
    assert signature() != first


def test_ocr_signature_uses_metadata_worker_not_live_engine(tmp_path, monkeypatch):
    import json
    import sys
    settings = SimpleNamespace(ocr_backend='paddle', ingest_ocr_dpi=150, ingest_ocr_language='eng',
        ocr_line_overlap=0.5, ocr_table_min_columns=2, ocr_worker_extra_sites='',
        ocr_cuda_dll_dir=None, ocr_weights_dir=tmp_path, data_dir=tmp_path,
        paddle_ocr_device='synthetic-device', paddle_ocr_language='synthetic-language',
        paddle_ocr_max_side=100, ocr_worker_python=sys.executable, ocr_timeout_seconds=10)
    monkeypatch.setattr(ocr, 'get_settings', lambda: settings)
    monkeypatch.setattr(ocr, '_get_worker', lambda: pytest.fail('live engine must not start'))
    def run(command, **kwargs):
        assert command[-1] == '--signature-only'
        assert json.loads(kwargs['input'])['device'] == 'synthetic-device'
        return SimpleNamespace(stdout=json.dumps({'fingerprint': 'synthetic-signature'}))
    monkeypatch.setattr(ocr.subprocess, 'run', run)
    assert ocr.ocr_signature()['worker_fingerprint'] == 'synthetic-signature'
