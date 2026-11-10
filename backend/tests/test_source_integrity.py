"""Synthetic frozen-file checks; no remote downloads or model calls."""
from hashlib import sha256
from app.services.tender.integrity import verify_sources
from app.services.tender.pipeline import run_pipeline, STAGE_ATTACHMENTS, STATUS_FAILED


def frozen(tmp_path, name='synthetic.pdf', content=b'synthetic source bytes', pages=2):
    path=tmp_path/name;path.write_bytes(content)
    return {'path':name,'sha256':sha256(content).hexdigest(),'pages':pages,'url':'synthetic://source'}


def test_changed_source_is_detected_before_any_cached_stage_or_llm(tmp_path):
    record=frozen(tmp_path);(tmp_path/record['path']).write_bytes(b'changed synthetic source')
    class Repo:
        def list_pages(self,*args):return []
        def list_attachments(self,*args):return [{'id':'synthetic-source','url':'synthetic://source'}]
        def replace_pages(self,tid,pages):self.pages=pages
        def save_stage(self,*args,**kwargs):self.stage=kwargs
        def stage_keys(self,*args):raise AssertionError('cache must not be read before source validation')
    class Client:
        def complete_json(self,*args):raise AssertionError('corrupt source must never reach model')
    repo=Repo();result=run_pipeline('synthetic-tender',tmp_path,[record],repo=repo,client=Client())
    assert result.failure=='source integrity check failed'
    assert result.stages[STAGE_ATTACHMENTS].status==STATUS_FAILED
    assert len(repo.pages)==2 and all(p['ledger_status']=='failed' for p in repo.pages)


def test_integrity_failure_preserves_unrelated_pages_and_existing_requirements(tmp_path):
    record=frozen(tmp_path);(tmp_path/record['path']).unlink()
    class Repo:
        def list_pages(self,*args):return [{'page':1,'ledger_status':'parsed','source_key':'unrelated'}]
        def list_attachments(self,*args):return [{'id':'synthetic-source','url':'synthetic://source'}]
        def replace_pages(self,tid,pages):self.pages=pages
        def save_stage(self,*args,**kwargs):pass
        def replace_requirements(self,*args):raise AssertionError('do not replace historical requirements')
        def replace_items(self,*args):raise AssertionError('do not replace historical items')
    repo=Repo();result=run_pipeline('synthetic-tender',tmp_path,[record],repo=repo)
    assert result.failure
    assert any(p['source_key']=='unrelated' and p['ledger_status']=='parsed' for p in repo.pages)


def test_hashes_are_checked_for_same_filename_and_extensionless_pdf(tmp_path):
    first=frozen(tmp_path,name='download',pages=2)
    second={**first,'sha256':sha256(b'other synthetic PDF').hexdigest()}
    assert verify_sources(tmp_path,[first])==[]
    assert verify_sources(tmp_path,[first,second])[0]['integrity_error']=='source_checksum_mismatch'


def test_wrapper_and_missing_checksum_cannot_bypass_validation(tmp_path):
    record=frozen(tmp_path)
    assert verify_sources(tmp_path,[],wrapper=record)==[]
    assert verify_sources(tmp_path,[{**record,'sha256':None}])[0]['integrity_error']=='missing_or_invalid_checksum'
    (tmp_path/record['path']).write_bytes(b'changed')
    assert verify_sources(tmp_path,[],wrapper=record)[0]['integrity_error']=='source_checksum_mismatch'


def test_new_matching_run_checks_sources_before_cached_run_reuse(tmp_path,monkeypatch):
    import json
    import pytest
    from app.core.config import get_settings
    from app.core.errors import ConflictError
    from app.services.output import runs
    directory=tmp_path/'tenders'/'synthetic-tender';directory.mkdir(parents=True)
    record=frozen(directory)
    wrapper=directory/'wrapper.pdf';wrapper.write_bytes(b'synthetic wrapper')
    (directory/'manifest.json').write_text(json.dumps({'wrapper_path':str(wrapper),
        'wrapper_sha256':sha256(wrapper.read_bytes()).hexdigest()}))
    (directory/record['path']).write_bytes(b'changed synthetic source')
    monkeypatch.setenv('DATA_DIR',str(tmp_path));get_settings.cache_clear()
    class Repo:
        def get_tender(self,tid):return {'status':'done'}
        def list_attachments(self,tid):return [record]
        def list_requirements(self,*args):raise AssertionError('do not use stale requirements')
    monkeypatch.setattr(runs,'TenderRepo',lambda session:Repo())
    with pytest.raises(ConflictError,match='source integrity'):
        runs.create_run(None,'synthetic-tender')


def test_old_extraction_cannot_be_used_as_a_new_matching_input():
    import pytest
    from app.core.errors import ConflictError
    from app.services.output.runs import _require_current_extraction
    from app.services.requirement.extract import PROMPT_VERSION
    with pytest.raises(ConflictError,match='refreshed'):
        _require_current_extraction([{'prompt_version':'synthetic-old-extraction'}])
    with pytest.raises(ConflictError,match='refreshed'):
        _require_current_extraction([{'prompt_version':None}])
    _require_current_extraction([{'prompt_version':PROMPT_VERSION}])


def test_declared_hash_without_source_path_is_not_silently_ignored(tmp_path):
    issues=verify_sources(tmp_path,[{'sha256':sha256(b'synthetic').hexdigest(),'path':None}])
    assert issues[0]['integrity_error']=='missing_source_path'
