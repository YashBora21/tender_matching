"""Disposable native PDF context cache, bound to verified frozen source bytes."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

import pymupdf
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

READER_VERSION = 'native-page-context-1'


class SourceIntegrityError(ValueError):
    """Frozen bytes are unavailable or disagree with their recorded identity."""


class NativeLine(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    line_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    bbox: list[FiniteFloat] = Field(min_length=4, max_length=4)
    font_size: FiniteFloat = Field(gt=0)

    @model_validator(mode='after')
    def ordered_box(self):
        if not self.bbox[0] < self.bbox[2] or not self.bbox[1] < self.bbox[3]:
            raise ValueError('invalid native line coordinates')
        return self


class Context(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    tables: list[list[list[str | None]]]
    native_lines: list[NativeLine]

    @model_validator(mode='after')
    def unique_lines(self):
        if len({line.line_id for line in self.native_lines}) != len(self.native_lines):
            raise ValueError('duplicate native line identity')
        return self


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def _validated(entry, identity):
    if not isinstance(entry, dict) or entry.get('identity') != identity:
        raise ValueError('page context identity mismatch')
    data = entry['data']
    if entry.get('payload_sha256') != sha256(_encoded(data)).hexdigest():
        raise ValueError('page context payload checksum mismatch')
    return Context.model_validate(data).model_dump(mode='json')


def _parse(source, page_number):
    with pymupdf.open(stream=source, filetype='pdf') as pdf:
        page = pdf[page_number - 1]
        return {'tables': [table.extract() for table in page.find_tables().tables],
                'native_lines': [
                    {'line_id': f'{block_index}:{line_index}', 'bbox': list(line['bbox']),
                     'text': ''.join(span['text'] for span in line['spans']),
                     'font_size': max(span['size'] for span in line['spans'] if span['text'].strip())}
                    for block_index, block in enumerate(page.get_text('dict')['blocks'])
                    for line_index, line in enumerate(block.get('lines', []))
                    if tuple(line.get('dir', ())) == (1.0, 0.0)
                    and any(span['text'].strip() for span in line['spans'])]}


def load_page_context(path: Path, page_number: int, expected_sha256: str, cache_dir: Path,
                      binding_policy: str, *, memory=None):
    """Verify the source before every memory/disk hit; parse the verified bytes."""
    if type(page_number) is not int or page_number < 1:
        raise ValueError('invalid source page number')
    if not isinstance(expected_sha256, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256):
        raise SourceIntegrityError('missing or invalid source checksum')
    try:
        source = path.read_bytes()
    except OSError as exc:
        raise SourceIntegrityError('source bytes unavailable') from exc
    actual = sha256(source).hexdigest()
    if actual != expected_sha256.lower():
        raise SourceIntegrityError('source checksum mismatch')
    identity = {'source_sha256': actual, 'page': page_number, 'reader': READER_VERSION,
                'pymupdf': pymupdf.VersionBind, 'binding_policy': binding_policy}
    key = sha256(_encoded(identity)).hexdigest()
    target = cache_dir / (key + '.json')
    for location in ('memory', 'disk'):
        try:
            entry = memory[key] if location == 'memory' and memory is not None else (
                json.loads(target.read_text(encoding='utf-8')) if location == 'disk' else None)
            data = _validated(entry, identity)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            continue
        if memory is not None:
            memory[key] = deepcopy(entry)
        return {**data, 'cache': location}
    # Parse failures propagate and are never persisted as an empty successful page.
    data = Context.model_validate(_parse(source, page_number)).model_dump(mode='json')
    entry = {'identity': identity, 'data': data, 'payload_sha256': sha256(_encoded(data)).hexdigest()}
    if memory is not None:
        memory[key] = deepcopy(entry)
    temporary = None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache_dir, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(_encoded(entry))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError:
        pass  # A disposable cache must not make a valid source unreadable.
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return {**deepcopy(data), 'cache': 'miss'}
