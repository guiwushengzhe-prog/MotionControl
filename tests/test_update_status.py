"""更新看得见：换没换包、成没成、哪些更新要告诉人。"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

from motioncontrol import app_update
from motioncontrol.update_status import UpdateStatus
from motioncontrol_shared.changelog import release_kind, releases_between, version_key
from test_configuration_coordination import application  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent

FEATURE = "### 新增\n\n- **扫码连接。** 手机扫一下就连上。\n"
FIX_ONLY = "### 修复\n\n- 断线后按键会松开。\n"


def changelog(*entries: tuple[str, str]) -> str:
    """entries: (version, body)，新的在前。"""
    return "# 更新日志\n\n" + "".join(f"## {version} — 2026-10-0{index + 1}\n\n{body}\n"
                                      for index, (version, body) in enumerate(entries))


def make_app(root: Path, version: str, log: str = "", digest: str = "") -> Path:
    app = root / "app"
    (app / "motioncontrol").mkdir(parents=True, exist_ok=True)
    (app / "motioncontrol" / "version.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    if log:
        (app / "CHANGELOG.md").write_text(log, encoding="utf-8")
    if digest:
        (app / ".complete").write_text(digest, encoding="utf-8")
    return app


def status(root: Path, version: str, clock=lambda: 1_700_000_000.0) -> UpdateStatus:
    return UpdateStatus(root / "app", root / "user" / "update_history.json", version, clock=clock)


def test_changelog_decides_which_updates_are_worth_telling():
    feature = {"sections": [{"title": "新增"}, {"title": "修复"}]}
    assert release_kind(feature) == "feature"
    assert release_kind({"sections": [{"title": "修复"}]}) == "system"
    assert release_kind({"sections": []}) == "system"
    assert version_key("2.10.0") > version_key("2.9.3") > version_key("bad")
    releases = [{"version": v, "channel": c} for v, c in
                [("1.10.0", "app"), ("1.9.1", "web"), ("1.9.0", "app"), ("1.8.0", "app")]]
    assert [r["version"] for r in releases_between(releases, "1.8.0", "1.10.0")] == ["1.10.0", "1.9.0"]


def test_a_fresh_install_records_its_version_without_a_popup(tmp_path):
    make_app(tmp_path, "1.9.0", changelog(("1.9.0", FEATURE)))
    updates = status(tmp_path, "1.9.0")
    updates.observe_launch()
    payload = updates.payload()
    assert payload["notes"] is None
    assert payload["events"] == []
    assert payload["updated_at"] is None
    saved = json.loads((tmp_path / "user" / "update_history.json").read_text(encoding="utf-8"))
    assert saved["seen_version"] == "1.9.0"


def test_a_feature_update_is_announced_once_and_remembered(tmp_path):
    make_app(tmp_path, "1.8.0", changelog(("1.8.0", FEATURE)), digest="old")
    status(tmp_path, "1.8.0").observe_launch()

    make_app(tmp_path, "1.9.0", changelog(("1.9.0", FEATURE), ("1.8.0", FEATURE)), digest="new")
    updates = status(tmp_path, "1.9.0", clock=lambda: 1_700_100_000.0)
    updates.observe_launch()
    payload = updates.payload()
    assert payload["events"][-1] == {"type": "updated", "from": "1.8.0", "to": "1.9.0", "at": 1_700_100_000.0}
    assert payload["updated_at"] == 1_700_100_000.0
    assert [r["version"] for r in payload["notes"]["releases"]] == ["1.9.0"]
    assert payload["notes"]["releases"][0]["kind"] == "feature"

    updates.acknowledge("1.9.0")
    assert updates.payload()["notes"] is None
    again = status(tmp_path, "1.9.0")
    again.observe_launch()
    assert again.payload()["notes"] is None, "看过的说明不再弹"
    assert len(again.payload()["events"]) == 1, "同一次更新不重复记"


def test_a_maintenance_update_is_recorded_but_stays_quiet(tmp_path):
    make_app(tmp_path, "1.9.0", changelog(("1.9.0", FEATURE)), digest="a")
    status(tmp_path, "1.9.0").observe_launch()
    make_app(tmp_path, "1.9.1", changelog(("1.9.1", FIX_ONLY), ("1.9.0", FEATURE)), digest="b")
    updates = status(tmp_path, "1.9.1")
    updates.observe_launch()
    payload = updates.payload()
    assert payload["notes"] is None
    assert payload["events"][-1]["to"] == "1.9.1"
    saved = json.loads((tmp_path / "user" / "update_history.json").read_text(encoding="utf-8"))
    assert saved["seen_version"] == "1.9.1"


def test_a_same_version_rebuild_counts_as_an_update_without_notes(tmp_path):
    make_app(tmp_path, "1.9.0", changelog(("1.9.0", FEATURE)), digest="first")
    status(tmp_path, "1.9.0").observe_launch()
    make_app(tmp_path, "1.9.0", changelog(("1.9.0", FEATURE)), digest="hotfix")
    updates = status(tmp_path, "1.9.0")
    updates.observe_launch()
    assert updates.payload()["events"][-1]["from"] == updates.payload()["events"][-1]["to"] == "1.9.0"
    assert updates.payload()["notes"] is None


def test_skipped_versions_are_listed_together_newest_first(tmp_path):
    make_app(tmp_path, "1.7.0", changelog(("1.7.0", FEATURE)), digest="a")
    status(tmp_path, "1.7.0").observe_launch()
    make_app(tmp_path, "1.9.0", changelog(("1.9.0", FIX_ONLY), ("1.8.0", FEATURE), ("1.7.0", FEATURE)), digest="b")
    updates = status(tmp_path, "1.9.0")
    updates.observe_launch()
    assert [r["version"] for r in updates.payload()["notes"]["releases"]] == ["1.9.0", "1.8.0"]


def test_the_first_update_carrying_this_feature_reads_the_old_version_from_the_backup(tmp_path):
    # 老安装还没有更新记录；刚换完包时旧的那份还在 app_previous，记号也还在。
    make_app(tmp_path, "1.9.0", changelog(("1.9.0", FEATURE), ("1.8.0", FEATURE)), digest="new")
    previous = tmp_path / "app_previous" / "motioncontrol"
    previous.mkdir(parents=True)
    (previous / "version.py").write_text('VERSION = "1.8.0"\n', encoding="utf-8")
    (tmp_path / "app.booting").write_text("1", encoding="utf-8")
    updates = status(tmp_path, "1.9.0")
    updates.observe_launch()
    payload = updates.payload()
    assert payload["events"][-1]["from"] == "1.8.0"
    assert [r["version"] for r in payload["notes"]["releases"]] == ["1.9.0"]


def test_a_rolled_back_launch_is_reported_once_and_not_as_an_update(tmp_path):
    make_app(tmp_path, "1.8.0", changelog(("1.8.0", FEATURE)), digest="old")
    status(tmp_path, "1.8.0").observe_launch()
    (tmp_path / "app_update_result.json").write_text(json.dumps(
        {"result": "rolled-back", "at": 1_700_200_000.0, "from": "1.9.0", "to": "1.8.0"}), encoding="utf-8")
    updates = status(tmp_path, "1.8.0")
    updates.observe_launch()
    events = updates.payload()["events"]
    assert [event["type"] for event in events] == ["rolled_back"]
    assert events[-1]["from"] == "1.9.0"
    repeat = status(tmp_path, "1.8.0")
    repeat.observe_launch()
    assert len(repeat.payload()["events"]) == 1


def test_a_staged_update_says_whether_it_brings_features(tmp_path):
    make_app(tmp_path, "1.8.0", changelog(("1.8.0", FEATURE)))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "launcher.py").write_text("", encoding="utf-8")
    updates = status(tmp_path, "1.8.0")
    assert updates.payload()["staged"] is None
    staging = tmp_path / "app_next"
    (staging / "motioncontrol").mkdir(parents=True)
    (staging / "motioncontrol" / "version.py").write_text('VERSION = "1.9.0"\n', encoding="utf-8")
    (staging / "CHANGELOG.md").write_text(changelog(("1.9.0", FEATURE), ("1.8.0", FEATURE)), encoding="utf-8")
    assert updates.payload()["staged"] is None, "没下完（没有完成记号）不算"
    (staging / ".complete").write_text("digest", encoding="utf-8")
    assert updates.payload()["staged"] == {"version": "1.9.0", "kind": "feature"}
    (staging / "CHANGELOG.md").write_text(changelog(("1.9.0", FIX_ONLY)), encoding="utf-8")
    assert updates.payload()["staged"]["kind"] == "system"


def test_source_checkouts_are_not_updatable_and_checks_do_not_overlap(tmp_path):
    make_app(tmp_path, "1.8.0")
    updates = status(tmp_path, "1.8.0")
    assert updates.payload()["updatable"] is False
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "launcher.py").write_text("", encoding="utf-8")
    assert updates.payload()["updatable"] is True
    assert updates.begin_check() is True
    assert updates.begin_check() is False
    assert updates.payload()["state"] == "checking"
    updates.finish_check({"state": "failed", "error": "网络不通"})
    assert updates.payload()["state"] == "failed" and updates.payload()["error"] == "网络不通"
    assert updates.begin_check() is True
    updates.finish_check({"state": "failed", "error": "<urlopen error Tunnel connection failed: 403 Forbidden>"})
    assert updates.payload()["error"] == "连不上更新服务器"
    assert "403" in updates.payload()["detail"]


def test_promote_records_what_happened_outside_app(tmp_path):
    app = make_app(tmp_path, "1.8.0")
    staging = tmp_path / "app_next"
    (staging / "motioncontrol").mkdir(parents=True)
    (staging / "motioncontrol" / "version.py").write_text('VERSION = "1.9.0"\n', encoding="utf-8")
    (staging / "server.py").write_text("", encoding="utf-8")
    (staging / ".complete").write_text("d", encoding="utf-8")
    assert app_update.promote(app) == "promoted"
    record = json.loads((tmp_path / app_update.RESULT_NAME).read_text(encoding="utf-8"))
    assert (record["result"], record["from"], record["to"]) == ("promoted", "1.8.0", "1.9.0")
    # 这一份没起来：下次启动退回，并且记下退回的是哪一版。
    assert app_update.promote(app) == "rolled-back"
    record = json.loads((tmp_path / app_update.RESULT_NAME).read_text(encoding="utf-8"))
    assert (record["result"], record["from"], record["to"]) == ("rolled-back", "1.9.0", "1.8.0")


def test_update_status_never_imports_from_app_update():
    """启动器换包前就导入了 app_update，换包后 import 到的是内存里的旧模块。

    update_status 若从那里拿新加的名字，带着这个功能的第一次更新就会 ImportError、
    起不来、被退回。
    """
    tree = ast.parse((ROOT / "motioncontrol" / "update_status.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "motioncontrol.app_update"
            assert not (node.module == "motioncontrol" and any(a.name == "app_update" for a in node.names))
        if isinstance(node, ast.Import):
            assert all(alias.name != "motioncontrol.app_update" for alias in node.names)


def test_changelog_ships_inside_the_release():
    from tools.stage_release import RUNTIME_FILES

    assert "CHANGELOG.md" in RUNTIME_FILES


def _request(app, route, replies, body=None, origin=None):
    request = object.__new__(app.AdminHandler)
    request._is_loopback = lambda: True
    request.path = route
    request.headers = {"Origin": origin} if origin else {}
    request.server = SimpleNamespace(server_port=8766)
    request._body = lambda: body or {}
    request._send_json = lambda data, status=200: replies.append((status, data))
    return request


def test_update_routes_report_and_acknowledge(application):
    app, replies = application, []
    _request(app, "/api/app-update", replies).do_GET()
    assert replies[-1][0] == 200 and replies[-1][1]["version"] == app.VERSION
    assert replies[-1][1]["updatable"] is False

    _request(app, "/api/app-update/check", replies).do_POST()
    assert replies[-1][0] == 409, "源码运行时不去下载"
    _request(app, "/api/app-update/seen", replies, {"version": app.VERSION},
             origin="https://evil.example").do_POST()
    assert replies[-1][0] == 403
    _request(app, "/api/app-update/seen", replies, {"version": app.VERSION},
             origin="http://127.0.0.1:8766").do_POST()
    assert replies[-1][0] == 200 and replies[-1][1]["notes"] is None

