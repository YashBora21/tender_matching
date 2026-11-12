"""Synthetic dotenv values replace stale local-terminal smoke-test overrides."""
import importlib.util
import os
from pathlib import Path


def test_local_launcher_uses_private_file_values(monkeypatch, tmp_path):
    module_path = Path(__file__).resolve().parents[2] / 'scripts' / 'dev_backend.py'
    spec = importlib.util.spec_from_file_location('dev_backend', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / 'scripts').mkdir()
    (tmp_path / '.env').write_text('LLM_CALL_BUDGET=600\nLLM_MAX_RETRIES=3\nLLM_TEXT_API_KEY=synthetic-file-key\n')
    monkeypatch.setattr(module, '__file__', str(tmp_path / 'scripts' / 'dev_backend.py'))
    for key in ('LLM_CALL_BUDGET', 'LLM_MAX_RETRIES', 'LLM_TEXT_API_KEY'):
        monkeypatch.setenv(key, '1')
    seen = {}
    monkeypatch.setattr(module.uvicorn, 'run', lambda *args, **kwargs: seen.update(
        calls=os.environ['LLM_CALL_BUDGET'], retries=os.environ['LLM_MAX_RETRIES'],
        key=os.environ['LLM_TEXT_API_KEY'], watch=kwargs['reload_dirs']))
    monkeypatch.syspath_prepend(str(tmp_path))
    module.main()
    assert (seen['calls'], seen['retries'], seen['key']) == ('600', '3', 'synthetic-file-key')
    assert seen['watch'] == [str(tmp_path / 'backend' / 'app')]
