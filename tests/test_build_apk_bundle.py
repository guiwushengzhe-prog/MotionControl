"""App 内更新用的 APK 下载包：版本号公式、功能 / 系统更新的判断、产物的样子。"""

from __future__ import annotations

import json

import pytest

from tools import build_apk_bundle as tool

CHANGELOG = """# 更新日志

## 网页 9.9.1 — 2026-01-03

### 新增

- 只是网页的新功能

## 9.9.0 — 2026-01-02

### 修复

- 修好一个原生问题

## 9.8.0 — 2026-01-01

### 新增

- **App 内更新。** 新版 APK 在 App 里就能装。
"""


def test_version_code_matches_the_phone_formula():
    assert tool.version_code("2.3.2") == 20302
    assert tool.version_code("3.10.12") == 31012
    with pytest.raises(SystemExit):
        tool.version_code("2.4")


def test_feature_releases_are_marked_so_the_phone_prompts_once():
    info = tool.apk_info("9.8.0", CHANGELOG)
    assert info["version_code"] == 90800
    assert info["kind"] == "feature"
    assert info["sections"][0]["title"] == "新增"


def test_fix_only_releases_stay_quiet_and_web_entries_do_not_count():
    info = tool.apk_info("9.9.0", CHANGELOG)
    assert info["kind"] == "system"
    assert info["sections"][0]["items"] == ["修好一个原生问题"]
    # 网页条目只随热更走，不算 APK 的说明。
    assert tool.apk_info("9.9.1", CHANGELOG) == {
        "version_name": "9.9.1", "version_code": 90901, "kind": "system", "sections": []}


def test_build_writes_the_two_files_the_phone_expects(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "OUT", tmp_path / "out")
    apk = tmp_path / "MotionControl-Android-9.8.0.apk"
    apk.write_bytes(b"PK" + b"\0" * 4096)
    tool.build(apk, "9.8.0", CHANGELOG)
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == ["MotionControl.apk", "apk.json"]
    assert (tmp_path / "out" / "MotionControl.apk").read_bytes() == apk.read_bytes()
    info = json.loads((tmp_path / "out" / "apk.json").read_text(encoding="utf-8"))
    assert info["version_name"] == "9.8.0" and info["kind"] == "feature"


def test_build_refuses_something_that_is_not_an_apk(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "OUT", tmp_path / "out")
    fake = tmp_path / "fake.apk"
    fake.write_text("not a zip" * 200, encoding="utf-8")
    with pytest.raises(SystemExit):
        tool.build(fake, "9.8.0", CHANGELOG)
    with pytest.raises(SystemExit):
        tool.build(tmp_path / "missing.apk", "9.8.0", CHANGELOG)
