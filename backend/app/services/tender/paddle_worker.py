"""Local JSON-lines OCR worker. No LLM, network API or business decisions."""
from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import sys
import time
from contextlib import redirect_stdout


def main():
    config = json.loads(sys.stdin.readline())
    for path in config.pop('extra_sites'):
        sys.path.append(path)
    dll = config.pop('dll_dir')
    dll_handle = os.add_dll_directory(dll) if dll and os.name == 'nt' else None
    if dll:
        os.environ['PATH'] = dll + os.pathsep + os.environ.get('PATH', '')
    weights = Path(config.pop('weights_dir'))
    os.environ['PADDLE_PDX_CACHE_HOME'] = str(weights)
    os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
    logging.disable(logging.CRITICAL)
    started = time.perf_counter()
    with redirect_stdout(sys.stderr):
        import cv2
        import numpy as np
        import paddle
        from paddleocr import PaddleOCR
        engine = PaddleOCR(**config, enable_mkldnn=False, use_doc_orientation_classify=True,
                           use_doc_unwarping=False, use_textline_orientation=False)
    digest = hashlib.sha256(Path(__file__).read_bytes())
    digest.update(json.dumps(config, sort_keys=True).encode())
    for package in ('paddleocr', 'paddlepaddle-gpu', 'paddlepaddle'):
        try:
            digest.update(importlib.metadata.version(package).encode())
        except importlib.metadata.PackageNotFoundError:
            pass
    for path in sorted(weights.rglob('*')):
        if path.is_file() and path.suffix in {'.pdiparams', '.pdmodel', '.json', '.yaml', '.yml', '.safetensors'}:
            digest.update(str(path.relative_to(weights)).encode())
            with path.open('rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
    print(json.dumps({'ready': True, 'fingerprint': digest.hexdigest(),
                      'setup_seconds': time.perf_counter() - started}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            image = cv2.imdecode(np.frombuffer(base64.b64decode(request['image']), dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('invalid image')
            started = time.perf_counter()
            with redirect_stdout(sys.stderr):
                predictions = list(engine.predict(image))
            if config['device'].startswith('gpu'):
                paddle.device.cuda.synchronize()
            if len(predictions) != 1:
                raise ValueError('expected one page')
            result = predictions[0]
            angle = int(result['doc_preprocessor_res']['angle'])
            transformed = result['doc_preprocessor_res']['output_img']
            h, w = image.shape[:2]
            oh, ow = transformed.shape[:2]
            matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1)
            matrix[0, 2] += (ow - w) / 2
            matrix[1, 2] += (oh - h) / 2
            inverse = cv2.invertAffineTransform(matrix)
            boxes = []
            for text, score, polygon in zip(result['rec_texts'], result['rec_scores'], result['rec_polys'], strict=True):
                polygon = np.asarray(polygon, dtype=float)
                source = cv2.transform(polygon[None], inverse)[0]
                boxes.append({'text': text, 'score': float(score), 'polygon': polygon.tolist(),
                              'source_polygon': source.tolist()})
            response = {'boxes': boxes, 'angle': angle, 'input_size': [w, h],
                        'output_size': [ow, oh], 'original_to_processed': matrix.tolist(),
                        'processed_to_original': inverse.tolist(), 'seconds': time.perf_counter() - started}
        except Exception as exc:
            response = {'error': type(exc).__name__}
        print(json.dumps(response), flush=True)
    return dll_handle


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__}), flush=True)
