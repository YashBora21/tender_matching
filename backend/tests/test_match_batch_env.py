"""Synthetic environment configuration; no model or database access."""
from app.core.config import Settings


def test_requested_match_batch_environment_name_takes_precedence(monkeypatch):
    monkeypatch.setenv('MATCH_EXTRACTION_BATCH_SIZE','2')
    monkeypatch.setenv('LLM_MATCH_EXTRACTION_BATCH_SIZE','8')
    assert Settings(_env_file=None).match_extraction_batch_size==8


def test_legacy_environment_and_python_field_name_still_work(monkeypatch):
    monkeypatch.delenv('LLM_MATCH_EXTRACTION_BATCH_SIZE',raising=False)
    monkeypatch.setenv('MATCH_EXTRACTION_BATCH_SIZE','3')
    assert Settings(_env_file=None).match_extraction_batch_size==3
    assert Settings(_env_file=None,match_extraction_batch_size=4).match_extraction_batch_size==4
