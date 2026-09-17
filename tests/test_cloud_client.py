"""The desktop's cloud client, and the checks that stand between it and the kernel.

The client downloads a config and hands it to code that rebinds every control.
So the interesting tests are not "does a download work" -- they are the ones
that prove a *wrong* download is refused. A cloud that is down is a nuisance; a
cloud that serves altered bytes and is believed is someone else controlling the
machine.

A stub HTTP server is used rather than the real service, because these tests
need to serve things the real service will not: a payload whose digest does not
match, a config the shared validator rejects, a body that never ends.
"""

from __future__ import annotations

import json
import threading
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cloud_client import MAX_DOWNLOAD_BYTES, CloudClient, CloudError, backup_user_data
from motioncontrol_shared.canonical import canonicalize

VALID_DOC = {
    "schema": "motioncontrol.profile_selection.v2",
    "selected_id": "generic-xbox",
    "overrides_by_profile": {
        "generic-xbox": {
            "zone.leftHandUpper": {"action": {"type": "gamepad", "target": "X",
                                              "behavior": "hold"}},
        },
    },
}


class _Stub(BaseHTTPRequestHandler):
    """Serves whatever the test put in ``server.script``."""

    def log_message(self, *args):  # keep pytest output readable
        pass

    def do_GET(self):
        payload, status, content_type = self.server.script(self.path)
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def stub():
    """A local HTTP server whose responses each test decides."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def make_script(document, *, digest=None, doc_type="profile_selection"):
    """Standard responses for one config, with the digest optionally a lie."""
    canonical = canonicalize(doc_type, document)
    reported = digest if digest is not None else canonical.sha256
    version = {"id": "v-1", "revision_no": 1, "canonical_sha256": reported,
               "schema_version": "x", "size_bytes": len(canonical.payload),
               "parent_version_id": None, "restored_from_id": None,
               "note": "", "created_at": "2026-01-01T00:00:00Z"}

    def script(path: str):
        if path.endswith("/download"):
            return canonical.payload, 200, "application/json"
        if "/versions" in path:
            return [version], 200, "application/json"
        return ({"id": "p-1", "doc_type": doc_type, "title": "测试配置",
                 "owner_name": "someone", "game_id": None, "summary": "",
                 "visibility": "public", "owner_id": "u-1",
                 "current_version": version,
                 "created_at": "2026-01-01T00:00:00Z",
                 "updated_at": "2026-01-01T00:00:00Z"}, 200, "application/json")
    return script


def client_for(stub) -> CloudClient:
    host, port = stub.server_address[:2]
    return CloudClient(f"http://{host}:{port}")


def test_a_good_config_downloads_and_validates(stub):
    stub.script = make_script(VALID_DOC)
    remote = client_for(stub).fetch("p-1")
    assert remote.doc_type == "profile_selection"
    assert remote.sha256 == canonicalize("profile_selection", VALID_DOC).sha256
    assert remote.document["selected_id"] == "generic-xbox"


def test_a_digest_that_does_not_match_is_refused(stub):
    """The one check that makes 'this is the published file' knowledge, not trust."""
    stub.script = make_script(VALID_DOC, digest="0" * 64)
    with pytest.raises(CloudError, match="校验值不一致"):
        client_for(stub).fetch("p-1")


def test_a_missing_digest_is_refused_rather_than_skipped(stub):
    """An absent value must not read as 'nothing to check'."""
    stub.script = make_script(VALID_DOC, digest="")
    with pytest.raises(CloudError, match="校验值不一致"):
        client_for(stub).fetch("p-1")


def test_a_config_the_local_rules_reject_never_reaches_the_kernel(stub):
    """The cloud is trusted to be available, not trusted to be correct.

    Here the stub reports a digest that matches the bytes it serves, so the
    transfer is intact -- and the config is still refused, because the local
    validator does not accept it. That is the property being tested: a cloud
    with a bug or a compromise cannot widen what this machine will run.
    """
    bad = json.loads(json.dumps(VALID_DOC))
    bad["overrides_by_profile"]["generic-xbox"]["zone.leftHandUpper"] = {
        "action": {"type": "gamepad", "target": "Z", "behavior": "hold"}}
    payload = json.dumps(bad).encode("utf-8")
    version = {"id": "v-1", "revision_no": 1,
               "canonical_sha256": sha256(payload).hexdigest(), "schema_version": "x",
               "size_bytes": len(payload), "parent_version_id": None,
               "restored_from_id": None, "note": "", "created_at": "2026-01-01T00:00:00Z"}

    def script(path: str):
        if path.endswith("/download"):
            return payload, 200, "application/json"
        if "/versions" in path:
            return [version], 200, "application/json"
        return ({"id": "p-1", "doc_type": "profile_selection", "title": "坏配置",
                 "owner_name": "x", "game_id": None, "current_version": version},
                200, "application/json")

    stub.script = script
    with pytest.raises(CloudError, match="不支持的 Xbox 按键：Z"):
        client_for(stub).fetch("p-1")


def test_an_oversized_body_is_refused(stub):
    """A hostile or broken server must not be able to exhaust memory."""
    stub.script = lambda path: (b"x" * (MAX_DOWNLOAD_BYTES + 100), 200, "application/json")
    with pytest.raises(CloudError, match="过大"):
        client_for(stub).fetch("p-1")


def test_an_unknown_document_type_is_refused(stub):
    stub.script = lambda path: (
        {"id": "p-1", "doc_type": "head_profile", "current_version": {"id": "v-1"}},
        200, "application/json")
    with pytest.raises(CloudError, match="不支持的配置类型"):
        client_for(stub).fetch("p-1")


def test_a_server_error_becomes_a_readable_message(stub):
    stub.script = lambda path: ({"detail": "配置不存在"}, 404, "application/json")
    with pytest.raises(CloudError, match="配置不存在"):
        client_for(stub).fetch("p-1")


def test_an_unreachable_cloud_raises_instead_of_hanging():
    # Port 1 on loopback refuses immediately; the point is that it raises
    # CloudError rather than propagating a socket error into the caller.
    with pytest.raises(CloudError, match="连不上云端"):
        CloudClient("http://127.0.0.1:1").health()


@pytest.mark.parametrize("url", ["", "ftp://x", "example.com", "ws://x"])
def test_only_http_urls_are_accepted(url):
    with pytest.raises(CloudError, match="http"):
        CloudClient(url)


# --- backup ------------------------------------------------------------------

def test_backup_copies_the_syncable_files_and_nothing_else(tmp_path):
    """Notably not scene_reference.jpg: it is large and nothing here changes it."""
    (tmp_path / "game_profile_selection.json").write_text("{}", encoding="utf-8")
    (tmp_path / "motion_mappings.json").write_text("{}", encoding="utf-8")
    (tmp_path / "scene_reference.jpg").write_bytes(b"not really a photo")
    (tmp_path / "head_profile.json").write_text("{}", encoding="utf-8")

    destination = backup_user_data(tmp_path)
    assert destination is not None
    names = {path.name for path in destination.iterdir()}
    assert names == {"game_profile_selection.json", "motion_mappings.json"}


def test_backup_of_an_empty_directory_reports_nothing_to_do(tmp_path):
    assert backup_user_data(tmp_path) is None


def test_the_desktop_default_endpoint_matches_the_deploy_config():
    """桌面端默认连的地址，必须和部署脚本里配的对外地址是同一个。

    这两处不一致过：子域名定下来之前 server.py 先写了 config.，实际定的是
    motioncontrol.，结果桌面端一直报 getaddrinfo failed——不是连不上，是那个域名
    根本不存在。错得很安静，只有真的去点一下才会发现。

    读源码而不是 import server：那个 import 会把整个控制内核和输出后端都拉起来，
    为了核对一行字符串不值得。
    """
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    desktop = re.search(r'DEFAULT_CLOUD_ENDPOINT = "([^"]+)"',
                        (repo / "server.py").read_text(encoding="utf-8"))
    deploy = re.search(r'SITE_ORIGIN=\$\{SITE_ORIGIN:-([^}]+)\}',
                       (repo / "cloud" / "deploy" / "bootstrap.sh").read_text(encoding="utf-8"))
    assert desktop and deploy, "两边的常量都要能找得到"
    assert desktop.group(1) == deploy.group(1), (
        f"桌面端默认连 {desktop.group(1)}，而部署到的是 {deploy.group(1)}")
