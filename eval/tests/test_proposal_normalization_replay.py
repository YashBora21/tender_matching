"""Offline contract checks over existing saved responses, not synthetic gold."""
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('proposal_replay', ROOT / 'eval/replay_proposal_normalization.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
MANIFEST = ROOT / 'eval/audit/source-normalization-2026-11-10/inputs.json'
INPUTS = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
pytestmark = pytest.mark.skipif(not INPUTS or any(not (ROOT / name).is_file()
    for name in INPUTS.get('files', {})), reason='frozen local replay inputs are not installed')


def test_saved_proposals_replay_without_claiming_accuracy():
    first = module.replay(MANIFEST)
    assert first == module.replay(MANIFEST)
    assert first['total_rows'] == 12
    assert first['raw_quotes_preserved']
    assert first['llm_calls'] == 0
    assert first['false_confident_claims'] is None
    assert first['gold_accuracy'] is None


def test_changed_input_hash_stops_replay(tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    manifest['files'][manifest['source_pdf']] = 'synthetic-nonmatching-hash'
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    with pytest.raises(AssertionError):
        module.replay(path)
