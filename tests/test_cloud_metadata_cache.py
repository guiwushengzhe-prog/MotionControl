"""Public metadata remains usable through slow refreshes, disconnects and restarts."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from motioncontrol.cloud_client import CloudClient, CloudError
from motioncontrol.cloud_metadata_cache import PublicMetadataCache
from test_cloud_client import stub


@pytest.fixture
def caches():
    made = []

    def create(*args, **kwargs):
        cache = PublicMetadataCache(*args, **kwargs)
        made.append(cache)
        return cache

    yield create
    for cache in made:
        cache.close()


def eventually(operation, predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        value = operation()
        if predicate(value):
            return value
        time.sleep(.005)
    pytest.fail("background refresh did not settle")


def test_simultaneous_first_reads_share_one_real_http_request(stub, caches):
    started, release = threading.Event(), threading.Event()
    calls = []

    def script(path):
        calls.append(path)
        started.set()
        assert release.wait(2)
        return [{"id": "shared", "title": "Public"}], 200, "application/json"

    stub.script = script
    endpoint = "http://%s:%s" % stub.server_address
    client = CloudClient(endpoint)
    cache = caches()
    try:
        with ThreadPoolExecutor(max_workers=6) as readers:
            results = [readers.submit(cache.read, endpoint, "browse", {"game_id": "one"},
                                      lambda: client.browse(game_id="one")) for _ in range(6)]
            assert started.wait(1)
            release.set()
            assert all(result.result()[0][0]["id"] == "shared" for result in results)
        assert len(calls) == 1
        assert cache.read(endpoint, "browse", {"game_id": "one"}, client.browse)[1]["cached"]
        assert len(calls) == 1
    finally:
        release.set()


def test_expired_value_returns_before_slow_background_refresh_and_updates_later(caches):
    now = [1000.0]
    cache = caches(clock=lambda: now[0], ttl_s=60)
    read = lambda loader: cache.read("https://one", "browse", {"game_id": "one"}, loader)
    initial, metadata = read(lambda: [{"id": "old"}])
    assert metadata["updated_at"] == 1000
    now[0] += 61
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return [{"id": "new"}]

    try:
        value, metadata = read(slow)
        assert started.wait(1)
        assert not release.is_set()
        assert value == initial and metadata["refreshing"] and metadata["stale"]
        assert metadata["updated_at"] == 1000
        for _ in range(10):
            assert read(slow)[0] == initial
        assert calls == [1], "stale readers launched duplicate cloud requests"
        release.set()
        value, metadata = eventually(lambda: read(slow), lambda result: not result[1]["refreshing"])
        assert value[0]["id"] == "new"
        assert metadata["updated_at"] == 1061 and not metadata["offline"]
    finally:
        release.set()


def test_offline_restart_retains_success_time_and_only_public_fields(tmp_path, caches):
    path = tmp_path / "metadata.json"
    now = [1000.0]
    endpoint = "https://user:secret-token@example.test/?token=private"
    cache = caches(path, clock=lambda: now[0])
    value, _metadata = cache.read(endpoint, "browse", {}, lambda: [{
        "id": "public", "title": "Visible", "token": "private-token", "document": {"password": "private-doc"},
        "current_version": {"id": "version", "revision_no": 1, "document": "private-version"},
    }])
    cache.close()
    text = path.read_text()
    assert not any(secret in text for secret in ("secret-token", "private-token", "private-doc", "private-version"))
    now[0] += 120
    restored = caches(path, clock=lambda: now[0])
    calls = []

    def offline():
        calls.append(1)
        raise CloudError("网络不通")

    assert restored.read(endpoint, "browse", {}, offline)[0] == value
    value, metadata = eventually(lambda: restored.read(endpoint, "browse", {}, offline),
                                  lambda result: result[1]["offline"])
    assert value[0]["id"] == "public"
    assert metadata["updated_at"] == 1000 and metadata["checked_at"] == 1120
    assert metadata["error"] == "网络不通" and metadata["stale"]
    assert calls == [1], "offline polling ignored retry backoff"


def test_endpoint_kind_and_query_parameters_do_not_share_snapshots(caches):
    cache = caches()
    for endpoint, game, title in (("https://one", "one", "a"), ("https://two", "one", "b"),
                                  ("https://one", "two", "c")):
        cache.read(endpoint, "browse", {"game_id": game, "doc_type": "motion_mappings"},
                   lambda title=title: [{"id": title}])
    for endpoint, game, title in (("https://one", "one", "a"), ("https://two", "one", "b"),
                                  ("https://one", "two", "c")):
        value, _metadata = cache.read(endpoint, "browse", {"doc_type": "motion_mappings", "game_id": game},
                                      lambda: pytest.fail("fresh metadata was fetched again"))
        assert value[0]["id"] == title
        value[0]["id"] = "caller mutation"
        assert cache.read(endpoint, "browse", {"game_id": game, "doc_type": "motion_mappings"}, lambda: [])[0][0]["id"] == title
    assert cache.read("https://one", "health", {}, lambda: {"ok": True})[0] == {"ok": True}


def test_first_offline_read_is_an_error_and_force_refresh_can_recover(caches):
    cache = caches()
    calls = []

    def offline():
        calls.append(1)
        raise CloudError("网络不通")

    for _ in range(2):
        with pytest.raises(CloudError, match="网络不通"):
            cache.read("https://one", "browse", {}, offline)
    assert calls == [1]
    value, _metadata = cache.read("https://one", "browse", {}, lambda: [], force=True)
    assert value == [], "a valid empty public list was treated as no cache"
    with pytest.raises(ValueError, match="public list"):
        cache.read("https://one", "profile", {}, lambda: {"document": "private"})


def test_cache_capacity_is_bounded_and_bad_disk_cache_does_not_block_first_read(tmp_path, caches):
    path = tmp_path / "metadata.json"
    path.write_text("{broken-json")
    cache = caches(path, max_entries=2)
    for game in ("one", "two", "three"):
        cache.read("https://one", "browse", {"game_id": game}, lambda: [])
    cache.close()
    assert len(json.loads(path.read_text())["entries"]) == 2


@pytest.mark.parametrize("wait", [False, True])
def test_small_cache_close_is_bounded_with_an_active_slow_loader(caches, wait):
    cache = caches(max_entries=1, wait_s=.03)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(2)
        return []

    try:
        with pytest.raises(CloudError, match="正在读取"):
            cache.read("https://one", "browse", {}, slow)
        assert started.is_set()
        closer = threading.Thread(target=lambda: cache.close(wait=wait), daemon=True)
        closer.start()
        closer.join(.5)
        assert not closer.is_alive(), "close blocked on its bounded worker queue"
        with pytest.raises(CloudError, match="已关闭"):
            cache.read("https://one", "browse", {}, lambda: [])
    finally:
        release.set()
        for worker in cache._workers:
            worker.join(.5)
        assert not any(worker.is_alive() for worker in cache._workers)
