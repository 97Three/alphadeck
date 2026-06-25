import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

from alphadeck_market_data.cache import MarketDataCache


def test_concurrent_writers_publish_complete_entries(tmp_path, monkeypatch):
    cache = MarketDataCache(root=tmp_path, enabled=True)
    barrier = Barrier(2)
    original_replace = Path.replace
    payloads = [{"writer": index, "data": str(index) * 100_000} for index in range(2)]

    def synchronized_replace(source, target):
        barrier.wait(timeout=5)
        result = original_replace(source, target)
        # Every published response remains complete while another writer finishes.
        assert cache.read("shared").payload in payloads
        return result

    monkeypatch.setattr(Path, "replace", synchronized_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(cache.write, "shared", ttl_seconds=60, payload=payload)
            for payload in payloads
        ]
        entries = [future.result(timeout=10) for future in futures]

    assert [entry.payload for entry in entries] == payloads
    assert cache.read("shared").payload in payloads
    assert list(tmp_path.iterdir()) == [tmp_path / "shared.json"]


def test_failed_replace_preserves_previous_entry_and_cleans_temporary_file(tmp_path, monkeypatch):
    cache = MarketDataCache(root=tmp_path, enabled=True)
    cache.write("shared", ttl_seconds=60, payload={"version": 1})

    def fail_replace(source, target):
        raise OSError("read-only cache directory")

    monkeypatch.setattr(Path, "replace", fail_replace)
    assert cache.write("shared", ttl_seconds=60, payload={"version": 2}) is None
    assert cache.read("shared").payload == {"version": 1}
    assert list(tmp_path.iterdir()) == [tmp_path / "shared.json"]


def test_expired_reader_does_not_delete_a_concurrently_refreshed_entry(tmp_path, monkeypatch):
    cache = MarketDataCache(root=tmp_path, enabled=True)
    original_read = Path.read_text
    cache.write("shared", ttl_seconds=60, payload={"version": 1})
    cache_file = tmp_path / "shared.json"
    # The reader captures an expired version just before a writer refreshes it.
    expired_record = json.loads(original_read(cache_file))
    expired_record["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    cache_file.write_text(json.dumps(expired_record), encoding="utf-8")

    def refresh_after_read(source, *args, **kwargs):
        raw = original_read(source, *args, **kwargs)
        cache.write("shared", ttl_seconds=60, payload={"version": 2})
        return raw

    monkeypatch.setattr(Path, "read_text", refresh_after_read)
    assert cache.read("shared") is None
    monkeypatch.setattr(Path, "read_text", original_read)
    assert cache.read("shared").payload == {"version": 2}


def test_disabled_or_zero_ttl_cache_does_not_create_files(tmp_path):
    root = tmp_path / "unused"
    disabled = MarketDataCache(root=root, enabled=False)
    assert disabled.write("shared", ttl_seconds=60, payload={}) is None
    assert disabled.read("shared") is None
    enabled = MarketDataCache(root=root, enabled=True)
    assert enabled.write("shared", ttl_seconds=0, payload={}) is None
    assert not root.exists()
