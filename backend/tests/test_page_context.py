"""Synthetic PDFs only: source integrity, persistent reuse and cache corruption."""
from hashlib import sha256
import json
import pytest
import pymupdf
from app.services.match import page_context as cache


def source(tmp_path):
    path = tmp_path/'synthetic.pdf'
    with pymupdf.open() as pdf:
        for text in ('Synthetic A; Coupling geometry: X7', 'Synthetic B; Coupling geometry: Y8'):
            pdf.new_page().insert_text((40, 50), text)
        pdf.save(path)
    return path, sha256(path.read_bytes()).hexdigest()


def load(tmp_path, path, digest, **kwargs):
    return cache.load_page_context(path, kwargs.pop('page', 1), digest,
                                   tmp_path/'cache', kwargs.pop('policy', 'synthetic-policy'), **kwargs)


def core(result):
    return {key: result[key] for key in ('tables', 'native_lines')}


def test_cold_disk_and_memory_hits_are_equivalent_without_reparsing(tmp_path, monkeypatch):
    path, digest = source(tmp_path)
    cold = load(tmp_path, path, digest)
    monkeypatch.setattr(cache, '_parse', lambda *args: pytest.fail('cached context was reparsed'))
    memory = {}
    disk = load(tmp_path, path, digest, memory=memory)
    hit = load(tmp_path, path, digest, memory=memory)
    assert [cold['cache'], disk['cache'], hit['cache']] == ['miss', 'disk', 'memory']
    assert core(cold) == core(disk) == core(hit)
    hit['native_lines'][0]['text'] = 'Synthetic caller mutation'
    assert core(load(tmp_path, path, digest, memory=memory)) == core(cold)


def test_changed_or_missing_source_is_rejected_before_memory_or_disk_hit(tmp_path):
    path, digest = source(tmp_path)
    memory = {}
    load(tmp_path, path, digest, memory=memory)
    path.write_bytes(path.read_bytes() + b'\n% synthetic revision\n')
    with pytest.raises(cache.SourceIntegrityError, match='checksum mismatch'):
        load(tmp_path, path, digest, memory=memory)
    new_digest = sha256(path.read_bytes()).hexdigest()
    assert load(tmp_path, path, new_digest, memory=memory)['cache'] == 'miss'
    path.unlink()
    with pytest.raises(cache.SourceIntegrityError, match='unavailable'):
        load(tmp_path, path, new_digest, memory=memory)


@pytest.mark.parametrize('digest', [None, '', 'synthetic-invalid-checksum'])
def test_invalid_source_identity_is_not_cached(tmp_path, digest):
    path, _ = source(tmp_path)
    with pytest.raises(cache.SourceIntegrityError): load(tmp_path, path, digest)
    assert not (tmp_path/'cache').exists()


@pytest.mark.parametrize('change', ['reader', 'engine', 'binding', 'page'])
def test_reader_engine_binding_and_page_changes_invalidate_context(tmp_path, monkeypatch, change):
    path, digest = source(tmp_path)
    load(tmp_path, path, digest)
    args = {}
    if change == 'reader': monkeypatch.setattr(cache, 'READER_VERSION', 'synthetic-reader-change')
    if change == 'engine': monkeypatch.setattr(cache.pymupdf, 'VersionBind', 'synthetic-engine-change')
    if change == 'binding': args['policy'] = 'synthetic-binding-change'
    if change == 'page': args['page'] = 2
    assert load(tmp_path, path, digest, **args)['cache'] == 'miss'
    assert len(list((tmp_path/'cache').glob('*.json'))) == 2


@pytest.mark.parametrize('damage', ['json', 'identity', 'checksum', 'schema', 'duplicate', 'coordinates', 'tables'])
def test_corrupt_entry_is_rebuilt_not_trusted(tmp_path, damage):
    path, digest = source(tmp_path)
    original = load(tmp_path, path, digest)
    target = next((tmp_path/'cache').glob('*.json'))
    entry = json.loads(target.read_text(encoding='utf-8'))
    if damage == 'json': target.write_text('{broken', encoding='utf-8')
    else:
        if damage == 'identity': entry['identity']['page'] = 2
        if damage == 'checksum': entry['data']['native_lines'][0]['text'] = 'Synthetic false value'
        if damage == 'schema': entry['data']['native_lines'][0]['font_size'] = float('inf')
        if damage == 'duplicate': entry['data']['native_lines'] *= 2
        if damage == 'coordinates': entry['data']['native_lines'][0]['bbox'] = [10, 10, 0, 0]
        if damage == 'tables': entry['data']['tables'] = [[[12]]]
        if damage in ('duplicate', 'coordinates', 'tables'):
            entry['payload_sha256'] = sha256(cache._encoded(entry['data'])).hexdigest()
        target.write_text(json.dumps(entry), encoding='utf-8')
    rebuilt = load(tmp_path, path, digest)
    assert rebuilt['cache'] == 'miss' and core(rebuilt) == core(original)
    assert load(tmp_path, path, digest)['cache'] == 'disk'


def test_parse_failure_does_not_write_a_successful_empty_context(tmp_path, monkeypatch):
    path, digest = source(tmp_path)
    def broken(*args): raise ValueError('synthetic parse failure')
    monkeypatch.setattr(cache, '_parse', broken)
    with pytest.raises(ValueError, match='synthetic parse failure'): load(tmp_path, path, digest)
    assert not (tmp_path/'cache').exists()


def test_cache_write_failure_preserves_reading_and_cleans_temporary_file(tmp_path, monkeypatch):
    path, digest = source(tmp_path)
    def blocked(*args): raise PermissionError('synthetic cache write failure')
    monkeypatch.setattr(cache.os, 'replace', blocked)
    result = load(tmp_path, path, digest)
    assert result['cache'] == 'miss' and result['native_lines']
    assert not list((tmp_path/'cache').iterdir())


def test_same_verified_bytes_at_another_path_can_share_context(tmp_path, monkeypatch):
    path, digest = source(tmp_path)
    expected = load(tmp_path, path, digest)
    other = tmp_path/'synthetic-renamed.pdf'
    other.write_bytes(path.read_bytes())
    monkeypatch.setattr(cache, '_parse', lambda *args: pytest.fail('identical source was reparsed'))
    assert core(load(tmp_path, other, digest)) == core(expected)


@pytest.mark.parametrize('page', [0, -1, True, 1.5, None, 99])
def test_invalid_or_out_of_bounds_page_does_not_persist_context(tmp_path, page):
    path, digest = source(tmp_path)
    with pytest.raises((ValueError, IndexError)):
        load(tmp_path, path, digest, page=page)
    assert not (tmp_path/'cache').exists()
