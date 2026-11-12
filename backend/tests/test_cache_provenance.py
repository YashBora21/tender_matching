"""Synthetic cache ownership; no provider calls or database writes."""
from app.repositories.tender import cache_provenance
from app.services.cli.reset import cache_exclusive


def test_reads_and_writes_accumulate_users_instead_of_last_writer_ownership():
    first = {'tender_id': 'synthetic-A', 'reading_id': '1'}
    second = {'tender_id': 'synthetic-B', 'reading_id': '2'}
    stamp = cache_provenance(None, first, new=True)
    assert cache_exclusive(stamp, ['synthetic-A'])
    shared = cache_provenance(stamp, second)
    assert shared['usages'] == [first, second]
    assert not cache_exclusive(shared, ['synthetic-A'])
    assert cache_exclusive(shared, ['synthetic-A', 'synthetic-B'])
    assert cache_provenance(shared, first) == shared


def test_unknown_previous_usage_and_corpus_use_permanently_prevent_deletion():
    usage = {'tender_id': 'synthetic-A', 'reading_id': '1'}
    for legacy in [None, {'tender_id': 'synthetic-A'}]:
        assert not cache_exclusive(cache_provenance(legacy, usage), ['synthetic-A'])
    stamp = cache_provenance(None, usage, new=True)
    stamp = cache_provenance(stamp, None)
    stamp = cache_provenance(stamp, usage)
    assert not cache_exclusive(stamp, ['synthetic-A'])
