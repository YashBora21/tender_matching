"""Synthetic native lines; no vendor-specific headings or inferred columns."""
import pytest
from app.services.match.prose_binding import bind_inline_model


def line(text, line_id='synthetic:1', bbox=None):
    return {'text': text, 'line_id': line_id, 'bbox': bbox if bbox is not None else [10, 10, 300, 25]}


def bind(lines, **changes):
    kwargs = dict(model='Synthetic Z9', labels=['coupling geometry'], value='X7', quote='X7')
    kwargs.update(changes)
    return bind_inline_model(lines, **kwargs)


def test_explicit_inline_model_attribute_and_value_retains_native_anchor():
    source = line('Synthetic Z9; Coupling geometry: X7')
    anchor = bind([source])
    assert anchor['line_id'] == source['line_id'] and anchor['bbox'] == source['bbox']
    assert source['text'] == anchor['text']


@pytest.mark.parametrize('text', [
    'Synthetic Y8; Coupling geometry: X7',
    'Synthetic Z9; Other geometry: X7',
    'Synthetic Z9; Coupling geometry: X70',
    'Synthetic Z90; Coupling geometry: X7',
    'Synthetic Z9 / Synthetic Y8; Coupling geometry: X7',
    'Synthetic Z9; Other geometry: X7; Coupling geometry: Y8',
    'Synthetic Z9; Synthetic Y8; Coupling geometry: X7',
    'Synthetic Z9; Coupling geometry: X7 optional',
])
def test_wrong_scope_attribute_value_or_ambiguous_line_is_not_bound(text):
    assert bind([line(text)]) is None


def test_page_wide_model_and_quote_do_not_bind_across_lines_or_columns():
    assert bind([line('Synthetic Z9'), line('Coupling geometry: X7', 'synthetic:2')]) is None


def test_repeated_anchors_remain_ambiguous():
    text = 'Synthetic Z9; Coupling geometry: X7'
    assert bind([line(text), line(text, 'synthetic:2')]) is None


def test_numeric_units_and_exact_conditions():
    source = line('Synthetic Z9 | Pressure = 12 bar')
    kwargs = dict(labels=['pressure'], value='12', quote='12 bar', unit='bar')
    assert bind([source], **kwargs)
    assert bind([source], **kwargs, conditions='Synthetic upgrade only') is None


@pytest.mark.parametrize('bbox', [[], [0, 0, 0, 10], [10, 10, 0, 0], [0, 0, float('nan'), 10]])
def test_invalid_native_coordinates_do_not_create_an_anchor(bbox):
    assert bind([line('Synthetic Z9; Coupling geometry: X7', bbox=bbox)]) is None


@pytest.mark.parametrize('lines', [None, {}, ['not a source line'], [dict(line('Synthetic Z9; Coupling geometry: X7'), bbox=5)]])
def test_malformed_stored_layout_remains_unbound(lines):
    assert bind(lines) is None


def test_source_anchor_gate_rejects_legacy_or_unbound_non_table_context():
    from app.services.match.verify import check_source_anchor
    context = {'tables': [], 'passage': 'Synthetic Z9; Coupling geometry: X7',
        'claim': {'model_or_series': 'Synthetic Z9', 'quote': 'X7'}}
    assert not check_source_anchor(context, 'coupling geometry', 'X7').passed
    context['native_lines'] = [line(context['passage'])]
    assert check_source_anchor(context, 'coupling geometry', 'X7') is None
    assert context['resolved_anchor']['line_id'] == 'synthetic:1'


@pytest.mark.parametrize('native_model,expected', [('Synthetic Z9', 'full_match'), ('Synthetic Y8', 'no_evidence_found'), (None, 'no_evidence_found')])
def test_actual_matching_path_gates_model_binding(monkeypatch, native_model, expected):
    from types import SimpleNamespace
    from app.services.match import match
    from app.services.match.retrieve import Candidate
    from app.services.match.facts import ExtractedFact, FactResult
    text = 'Synthetic Z9; End connection: Flanged'
    context = {'tables': [], 'passage': text, 'document_model': 'Synthetic Z9',
        'native_lines': [] if native_model is None else [line(f'{native_model}; End connection: Flanged')]}
    monkeypatch.setattr(match, '_cached_fact', lambda *args: None)
    monkeypatch.setattr(match, '_source_tables', lambda *args: [])
    monkeypatch.setattr(match, '_evidence_context', lambda *args: context)
    monkeypatch.setattr(match, '_page_text', lambda *args: text)
    fact = ExtractedFact(found=True, attribute='end connection', value='Flanged', operator='eq',
                         model='Synthetic Z9', quote='Flanged')
    result = match._match_candidate(SimpleNamespace(),
        Candidate(chunk_id='synthetic', document_id='synthetic', text=text, page=1,
                  model='Synthetic Z9', manufacturer='Synthetic'),
        'End connection flanged', requirement_value='flanged', requirement_operator='eq',
        extracted=FactResult(facts=[fact]))
    assert result.status.value == expected
    if expected != 'full_match':
        assert result.review_required and result.verification_outcome.value == 'failed'
    else:
        assert result.source_context['resolved_anchor']['line_id'] == 'synthetic:1'


def test_list_exclusion_cannot_publish_missing_from_page_wide_model_metadata(monkeypatch):
    from types import SimpleNamespace
    from app.services.match import match
    from app.services.match.retrieve import Candidate
    from app.services.match.facts import FactResult
    text = 'Body Style: Flanged end / Butt weld end'
    monkeypatch.setattr(match, '_cached_fact', lambda *args: None)
    monkeypatch.setattr(match, '_source_tables', lambda *args: [])
    monkeypatch.setattr(match, '_evidence_context', lambda *args: {
        'tables': [], 'passage': text, 'document_model': 'Synthetic Z9', 'native_lines': [line(text)]})
    monkeypatch.setattr(match, '_page_text', lambda *args: text)
    result = match._match_candidate(SimpleNamespace(),
        Candidate(chunk_id='synthetic', document_id='synthetic', text=text, page=1,
                  model='Synthetic Z9', manufacturer='Synthetic'),
        'Screwed end', extracted=FactResult(facts=[]))
    assert result.status.value == 'no_evidence_found'
    assert result.review_required and result.verification_outcome.value == 'failed'


def test_native_line_capture_keeps_coordinates_and_reuses_page_cache(tmp_path, monkeypatch):
    import pymupdf
    from app.services.match import match
    from app.services.match.retrieve import Candidate
    path = tmp_path/'synthetic-prose.pdf'
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    from hashlib import sha256
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 50), 'Synthetic Z9; Coupling geometry: X7')
        pdf.save(path)
    class Session:
        info = {}
        reads = 0
        def execute(self, query):
            self.reads += 1
            class Row:
                def first(self): return (str(path), sha256(path.read_bytes()).hexdigest())
            return Row()
    session = Session()
    candidate = Candidate(chunk_id='synthetic', document_id='synthetic', text='Synthetic', page=1)
    assert match._source_tables(session, candidate) == []
    native = session.info['verification_native_lines'][('synthetic', 1)]
    assert bind(native)
    assert native[0]['bbox'][0] == 40
    assert match._source_tables(session, candidate) == [] and session.reads == 1


@pytest.mark.parametrize('batch', [False, True])
def test_native_geometry_is_preserved_locally_without_expanding_model_context(batch):
    from app.services.match.facts import extract_fact, extract_fact_batch
    context = {'page': 1, 'native_lines': [line('SYNTHETIC_GEOMETRY_ONLY')], 'tables': []}
    class Client:
        def complete_json(self, system, user, **kwargs):
            assert 'SYNTHETIC_GEOMETRY_ONLY' not in user and 'native_lines' not in user
            return {'passages': [{'passage_id': 0, 'requirements': []}]} if batch else {'requirements': []}
    if batch:
        assert not extract_fact_batch('Synthetic', ['Synthetic passage'], source_contexts=[context], client=Client())[0].error
    else:
        assert not extract_fact('Synthetic', 'Synthetic passage', source_context=context, client=Client()).error
    assert context['native_lines'][0]['text'] == 'SYNTHETIC_GEOMETRY_ONLY'
