"""Validate frozen bytes before parsing or trusting cached tender results."""
from hashlib import file_digest
from pathlib import Path
import re


def source_error(path: Path, expected):
    if not expected or not re.fullmatch(r'[0-9a-fA-F]{64}', str(expected)):
        return 'missing_or_invalid_checksum'
    try:
        with path.open('rb') as stream:
            actual = file_digest(stream, 'sha256').hexdigest()
    except OSError:
        return 'source_unreadable'
    return None if actual == expected.lower() else 'source_checksum_mismatch'


def verify_sources(directory, attachments, *, wrapper=None):
    issues=[]
    sources=[r for r in attachments if r.get('path') or r.get('sha256')]
    if wrapper is not None:
        sources.append(wrapper)
    for record in sources:
        path=directory / record['path'] if record.get('path') else None
        error=source_error(path,record.get('sha256')) if path is not None else 'missing_source_path'
        if error:
            issues.append({**record,'integrity_error':error})
    return issues


def tender_source_issues(tender_id, attachments):
    from app.core.config import get_settings
    from app.services.tender.collect import read_manifest
    from app.services.tender.upload import _wrapper
    directory = get_settings().data_dir / 'tenders' / str(tender_id)
    manifest_path = directory / 'manifest.json'
    if not manifest_path.exists():
        return [{'integrity_error': 'missing_source_manifest'}]
    manifest = read_manifest(manifest_path)
    wrapper_path = _wrapper(directory, manifest)
    wrapper = {'path': str((wrapper_path or directory / 'missing-wrapper').resolve()),
               'sha256': manifest.get('wrapper_sha256'), 'source_key': 'wrapper'}
    return verify_sources(directory, attachments, wrapper=wrapper)
