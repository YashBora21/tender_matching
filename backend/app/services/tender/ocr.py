"""Persistent isolated Paddle OCR, with source-derived readings cached on disk."""
from __future__ import annotations

import atexit
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from app.core.config import get_settings

_lock = threading.RLock()
_worker = None


def worker_config(settings):
    return {'extra_sites': [p for p in settings.ocr_worker_extra_sites.split(os.pathsep) if p],
            'dll_dir': str(settings.ocr_cuda_dll_dir or ''),
            'weights_dir': str(settings.ocr_weights_dir or settings.data_dir / 'tmp' / 'paddle-weights'),
            'device': settings.paddle_ocr_device, 'lang': settings.paddle_ocr_language,
            'text_det_limit_side_len': settings.paddle_ocr_max_side, 'text_det_limit_type': 'max'}


class Worker:
    def __init__(self, settings):
        if not settings.ocr_worker_python:
            raise RuntimeError('OCR_WORKER_PYTHON is not configured')
        self.signature = worker_config(settings)
        self.signature['python'] = str(settings.ocr_worker_python)
        self.timeout = settings.ocr_timeout_seconds
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        self.process = subprocess.Popen([str(settings.ocr_worker_python), str(Path(__file__).with_name('paddle_worker.py'))],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding='utf-8', **options)
        self.responses = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self.ready = self.request(worker_config(settings))
            if not self.ready.get('ready'):
                raise RuntimeError('OCR worker did not initialize')
        except Exception:
            self.close()
            raise

    def _reader(self):
        for line in self.process.stdout:
            self.responses.put(line)
        self.responses.put(None)

    def request(self, payload):
        try:
            self.process.stdin.write(json.dumps(payload) + '\n')
            self.process.stdin.flush()
            line = self.responses.get(timeout=self.timeout)
            if line is None:
                raise RuntimeError('OCR worker stopped')
            result = json.loads(line)
            if result.get('error'):
                raise RuntimeError('OCR worker failed (' + result['error'] + ')')
            return result
        except Exception:
            self.close()
            raise

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)


def _get_worker():
    global _worker
    settings = get_settings()
    signature = {**worker_config(settings), 'python': str(settings.ocr_worker_python)}
    if _worker is None or _worker.process.poll() is not None or _worker.signature != signature:
        if _worker is not None:
            _worker.close()
        _worker = Worker(settings)
    return _worker


def warm_ocr():
    if get_settings().ocr_backend == 'paddle':
        with _lock:
            _get_worker()


def ocr_signature():
    settings = get_settings()
    signature = {'backend': settings.ocr_backend, 'dpi': settings.ingest_ocr_dpi,
                 'language': settings.ingest_ocr_language, 'source_schema': 'ocr-source-v1', 'line_overlap': settings.ocr_line_overlap,
                 'min_columns': settings.ocr_table_min_columns,
                 'layout_hash': hashlib.sha256(Path(__file__).with_name('ocr_layout.py').read_bytes()).hexdigest()}
    if settings.ocr_backend == 'paddle':
        with _lock:
            signature['worker_fingerprint'] = _get_worker().ready['fingerprint']
    return signature


@atexit.register
def close_ocr():
    global _worker
    with _lock:
        if _worker is not None:
            _worker.close()
            _worker = None


def validate_reading(result):
    w, h = result['input_size']
    if w <= 0 or h <= 0 or not result.get('boxes'):
        raise ValueError('OCR read no usable boxes')
    for box in result['boxes']:
        if not isinstance(box['text'], str) or not 0 <= box['score'] <= 1:
            raise ValueError('invalid OCR text or confidence')
        for key in ('polygon', 'source_polygon'):
            if len(box[key]) != 4 or any(len(point) != 2 or not all(math.isfinite(v) for v in point) for point in box[key]):
                raise ValueError('invalid OCR source geometry')
    return result


def read_paddle_page(page):
    started = time.perf_counter()
    settings = get_settings()
    image = page.get_pixmap(dpi=settings.ingest_ocr_dpi).tobytes('png')
    with _lock:
        worker = _get_worker()
        digest = hashlib.sha256(image)
        digest.update(worker.ready['fingerprint'].encode())
        digest.update(b'ocr-source-v1')
        key = digest.hexdigest()
        path = settings.data_dir / 'tmp' / 'ocr' / (key + '.json')
        hit = False
        if path.exists():
            try:
                result = validate_reading(json.loads(path.read_text(encoding='utf-8')))
                hit = True
            except (ValueError, KeyError, TypeError):
                result = None
        else:
            result = None
        if result is None:
            result = validate_reading(worker.request({'image': base64.b64encode(image).decode('ascii')}))
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f'.{key}-{os.getpid()}.tmp')
            temporary.write_text(json.dumps(result), encoding='utf-8')
            temporary.replace(path)
    # Cache only the pixel reading; attach the current document's coordinates afterwards.
    result = json.loads(json.dumps(result))
    w, h = result['input_size']
    for box in result['boxes']:
        matrix = page.derotation_matrix
        points = [(x * page.rect.width / w, y * page.rect.height / h) for x, y in box['source_polygon']]
        box['pdf_polygon'] = [[matrix.a * x + matrix.c * y + matrix.e,
                               matrix.b * x + matrix.d * y + matrix.f] for x, y in points]
    result.update(cache_key=key, cache_hit=hit, worker_fingerprint=worker.ready['fingerprint'],
                  setup_seconds=worker.ready['setup_seconds'], page=page.number + 1,
                  pdf_rotation=page.rotation, dpi=settings.ingest_ocr_dpi,
                  elapsed_seconds=time.perf_counter() - started)
    return result
