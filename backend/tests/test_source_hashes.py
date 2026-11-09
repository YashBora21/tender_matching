"""Synthetic frozen bytes; no parsing, provider or database access."""
from hashlib import sha256
import pytest
from app.services.tender.integrity import source_error, verify_sources


def test_extensionless_source_is_verified_by_bytes_not_filename(tmp_path):
    content=b'synthetic frozen document';path=tmp_path/'download';path.write_bytes(content)
    record={'path':'download','sha256':sha256(content).hexdigest()}
    assert verify_sources(tmp_path,[record])==[]
    path.write_bytes(b'changed synthetic document')
    assert verify_sources(tmp_path,[record])[0]['integrity_error']=='source_checksum_mismatch'


@pytest.mark.parametrize('expected',[None,'synthetic-invalid-checksum'])
def test_unknown_or_invalid_checksum_cannot_be_trusted(tmp_path,expected):
    path=tmp_path/'synthetic.pdf';path.write_bytes(b'synthetic')
    assert source_error(path,expected)=='missing_or_invalid_checksum'


def test_missing_path_and_missing_bytes_are_distinct_failures(tmp_path):
    expected=sha256(b'synthetic').hexdigest()
    assert verify_sources(tmp_path,[{'sha256':expected}])[0]['integrity_error']=='missing_source_path'
    assert source_error(tmp_path/'absent.pdf',expected)=='source_unreadable'


def test_colliding_names_do_not_make_two_declared_hashes_valid(tmp_path):
    path=tmp_path/'download';path.write_bytes(b'synthetic second source')
    records=[{'path':'download','sha256':sha256(b'synthetic first source').hexdigest()},
             {'path':'download','sha256':sha256(path.read_bytes()).hexdigest()}]
    issues=verify_sources(tmp_path,records)
    assert len(issues)==1 and issues[0]['sha256']==records[0]['sha256']
