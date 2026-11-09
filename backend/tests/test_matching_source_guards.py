"""Synthetic source lineage at matching boundaries; no provider or database access."""
import json
from hashlib import sha256
import pytest
from app.core.config import get_settings
from app.core.errors import ConflictError
from app.services.output import runs


def test_run_creation_checks_source_before_cached_requirements(tmp_path,monkeypatch):
    directory=tmp_path/'tenders'/'synthetic-tender';directory.mkdir(parents=True)
    wrapper=directory/'wrapper.pdf';wrapper.write_bytes(b'synthetic wrapper')
    source=directory/'source.pdf';source.write_bytes(b'changed synthetic source')
    record={'path':'source.pdf','sha256':sha256(b'original synthetic source').hexdigest()}
    (directory/'manifest.json').write_text(json.dumps({'wrapper_path':str(wrapper),
        'wrapper_sha256':sha256(wrapper.read_bytes()).hexdigest()}))
    monkeypatch.setenv('DATA_DIR',str(tmp_path));get_settings.cache_clear()
    class Tender:
        def get_tender(self,tid):return {'status':'done'}
        def list_attachments(self,tid):return [record]
        def list_requirements(self,tid):raise AssertionError('stale requirements must not be read')
    monkeypatch.setattr(runs,'TenderRepo',lambda session:Tender())
    with pytest.raises(ConflictError,match='source integrity'):
        runs.create_run(None,'synthetic-tender')


def test_resumed_run_stops_before_claim_or_matching_on_bad_sources(monkeypatch):
    class Session:
        def rollback(self):pass
        def commit(self):pass
    class Repo:
        def get(self,run_id):return {'status':'rate_limited','tender_id':'synthetic','manufacturers':['Synthetic'],'error':None}
        def count_results(self,run_id):return 0
        def set_status(self,run_id,status,**kwargs):self.status=status.value
        def claim(self,*args):raise AssertionError('do not claim a run with invalid sources')
    class Tender:
        def list_requirements(self,tid):return []
    def forbidden(*a,**kw):raise AssertionError('do not match invalid sources')
    monkeypatch.setattr(runs,'_source_issues',lambda *a:[{'integrity_error':'source_checksum_mismatch'}])
    monkeypatch.setattr(runs,'_record_costs',lambda *a,**kw:None)
    repo=Repo()
    outcome=runs.execute_run('synthetic-run',session=Session(),repo=repo,tender_repo=Tender(),matcher=forbidden)
    assert outcome.status==repo.status=='failed'


def test_outdated_and_unversioned_extraction_cannot_become_new_matching_input():
    from app.services.requirement.extract import PROMPT_VERSION
    for old in (None,'synthetic-old-version'):
        with pytest.raises(ConflictError,match='refreshed'):
            runs._require_current_extraction([{'prompt_version':old}])
    runs._require_current_extraction([{'prompt_version':PROMPT_VERSION}])
