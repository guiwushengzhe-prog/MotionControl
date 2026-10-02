"""Exercise actual admin handlers with isolated public cache and local pose state."""

from types import SimpleNamespace

from motioncontrol.cloud_client import CloudError
from motioncontrol.cloud_metadata_cache import PublicMetadataCache
from test_cloud_metadata_cache import eventually
from test_configuration_coordination import application
from test_profile_api_v2 import request_for


def test_browse_keeps_offline_results_and_install_still_fetches_fresh(application, monkeypatch):
    app, replies, now, calls = application, [], [1000.0], []
    cache = PublicMetadataCache(clock=lambda: now[0])
    monkeypatch.setattr(app, "CLOUD_METADATA", cache)
    monkeypatch.setattr(app, "cloud_endpoint", lambda: "https://one")
    offline = [False]

    class Client:
        def __init__(self, endpoint): self.base = endpoint
        def browse(self, **parameters):
            calls.append(("browse", parameters))
            if offline[0]: raise CloudError("offline")
            return [{"id": "public", "title": "Last successful result"}]
        def health(self): raise AssertionError("browse requested unnecessary health check")
        def fetch(self, profile_id, version_id):
            calls.append(("fresh-download", profile_id))
            return SimpleNamespace()

    monkeypatch.setattr(app, "CloudClient", Client)
    monkeypatch.setattr(app, "_install_cloud_config", lambda remote, game_id: {"ok": True, "fresh": True})
    request = request_for(app, "/api/cloud/browse", replies)
    request._body = lambda: {"game_id": "game"}
    try:
        request.do_POST()
        assert replies[-1][0] == 200 and replies[-1][1]["profiles"][0]["id"] == "public"
        offline[0] = True
        now[0] += 61

        def browse():
            request.do_POST()
            return replies[-1][1]

        payload = eventually(browse, lambda result: result["cache"]["offline"])
        assert payload["profiles"][0]["title"] == "Last successful result"
        assert payload["cache"]["updated_at"] == 1000
        install = request_for(app, "/api/cloud/install", replies)
        install._body = lambda: {"profile_id": "public"}
        install.do_POST()
        assert replies[-1][1]["fresh"]
        assert ("fresh-download", "public") in calls
        monkeypatch.setattr(app, "cloud_endpoint", lambda: "https://two")
        request.do_POST()
        assert replies[-1][0] == 502 and "profiles" not in replies[-1][1]
    finally:
        cache.close()


def test_pose_cache_never_caches_installed_revision_or_crosses_endpoint(application, monkeypatch):
    app, replies, installed, calls = application, [], [0], []
    cache = PublicMetadataCache()
    monkeypatch.setattr(app, "CLOUD_METADATA", cache)
    endpoint = ["https://one"]
    monkeypatch.setattr(app, "cloud_endpoint", lambda: endpoint[0])
    monkeypatch.setattr(app.POSE_ACTIONS, "revision", lambda ident: installed[0])

    class Client:
        def __init__(self, endpoint): self.base = endpoint
        def pose_library(self):
            calls.append(self.base)
            return {"actions": [{"id": "pose", "name": self.base, "revision": 2,
                                 "installed_revision": 999, "document": "not display metadata"}]}

    monkeypatch.setattr(app, "CloudClient", Client)
    request = request_for(app, "/api/pose/cloud", replies)
    try:
        request.do_GET()
        assert replies[-1][1]["actions"][0]["installed_revision"] == 0
        installed[0] = 1
        request.do_GET()
        assert replies[-1][1]["actions"][0]["installed_revision"] == 1
        assert replies[-1][1]["actions"][0]["update_available"]
        assert calls == ["https://one"]
        endpoint[0] = "https://two"
        request.do_GET()
        assert replies[-1][1]["actions"][0]["name"] == "https://two"
        assert calls == ["https://one", "https://two"]
        assert "document" not in replies[-1][1]["actions"][0]
    finally:
        cache.close()


def test_endpoint_only_status_never_waits_for_cloud(application, monkeypatch):
    app, replies = application, []
    monkeypatch.setattr(app, "CloudClient", lambda endpoint: (_ for _ in ()).throw(AssertionError("network requested")))
    request = request_for(app, "/api/cloud/status?endpoint_only=1", replies)
    request.do_GET()
    assert replies[-1][0] == 200 and replies[-1][1]["endpoint"]


def test_offline_action_metadata_does_not_authorize_an_unsigned_install(application, monkeypatch):
    app, replies, now, downloads = application, [], [1000.0], []
    cache = PublicMetadataCache(clock=lambda: now[0])
    monkeypatch.setattr(app, "CLOUD_METADATA", cache)
    offline = [False]

    class Client:
        def __init__(self, endpoint): self.base = endpoint
        def pose_library(self):
            if offline[0]: raise CloudError("offline")
            return {"actions": [{"id": "squat", "name": "Public squat", "revision": 1}]}
        def pose_action(self, ident):
            downloads.append(ident)
            return "e30=", "bm90LWEtdmFsaWQtc2lnbmF0dXJl"

    monkeypatch.setattr(app, "CloudClient", Client)
    request = request_for(app, "/api/pose/cloud", replies)
    try:
        request.do_GET()
        offline[0], now[0] = True, 1061.0

        def read():
            request.do_GET()
            return replies[-1][1]

        payload = eventually(read, lambda result: result["cache"]["offline"])
        assert payload["cache"]["updated_at"] == 1000
        assert payload["actions"][0]["name"] == "Public squat"
        install = request_for(app, "/api/pose/cloud/install", replies)
        install._body = lambda: {"id": "squat"}
        install.do_POST()
        assert downloads == ["squat"]
        assert replies[-1][0] == 400 and "签名验不过" in replies[-1][1]["error"]
        assert app.POSE_ACTIONS.revision("squat") == 0
    finally:
        cache.close()
