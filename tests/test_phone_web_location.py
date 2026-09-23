"""手机的网页包，在发布包的布局里要找得到。

2.0 发出去的包里找不到：server.py 从包的顶层搬进 app/ 之后，这里还在 root 底下
找 phone_web，而它在 app/ 的上一级。失败是无声的——manifest 返回 0 个文件，手机
拿到一个空清单，以为自己已经是最新的，于是网页包的更新通道整个不存在，而两边
都不报错。

放在 app/ 外面是故意的：更新时 app/ 会被整个换掉，手机的网页包不该跟着一起换。
所以这条查找必须一直在，而且得有测试盯着。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def find(monkeypatch):
    monkeypatch.delenv("PHONE_WEB_DIR", raising=False)
    from server import find_phone_web

    return find_phone_web


def test_the_release_layout_is_found(find, tmp_path):
    """发布包：顶层是 app/、phone_web/、python/，server.py 在 app/ 里。"""
    app = tmp_path / "app"
    app.mkdir()
    bundle = tmp_path / "phone_web"
    bundle.mkdir()
    assert find(app) == bundle


def test_beside_server_py_still_wins(find, tmp_path):
    """并排放着的那份优先：开发时和别的布局都靠它。"""
    app = tmp_path / "app"
    (app / "phone_web").mkdir(parents=True)
    (tmp_path / "phone_web").mkdir()
    assert find(app) == app / "phone_web"


def test_the_env_override_beats_both(find, tmp_path, monkeypatch):
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    (tmp_path / "app" / "phone_web").mkdir(parents=True)
    monkeypatch.setenv("PHONE_WEB_DIR", str(elsewhere))
    assert find(tmp_path / "app") == elsewhere


def test_the_sibling_repo_is_the_last_resort(find, tmp_path):
    """开发时两个仓库并排放着。"""
    dist = tmp_path / "switch" / "mobile" / "dist"
    dist.mkdir(parents=True)
    assert find(tmp_path / "MotionControl-App") == dist


def test_current_mobile_worktree_wins_over_old_build(find, tmp_path):
    current = tmp_path / "MC-switch" / "mobile" / "dist"
    old = tmp_path / "switch" / "mobile" / "dist"
    current.mkdir(parents=True)
    old.mkdir(parents=True)
    assert find(tmp_path / "MC-main") == current


def test_nothing_anywhere_is_not_a_crash(find, tmp_path):
    assert find(tmp_path / "app") is None
