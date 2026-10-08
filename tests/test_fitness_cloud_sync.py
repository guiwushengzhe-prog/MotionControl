"""电脑登录云端账号、运动记录换台电脑还在：真起一个云端服务来跑一遍。

云端用临时数据库、本机随便一个端口，在子进程里跑——电脑这边走的是真的 urllib，
和用户机器上一模一样。云端的接口细节在 cloud/tests/test_device_fitness.py 里测，
这里只看电脑这一半接得上：登录、两台电脑之间搬记录、退出。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("aiosqlite")

from motioncontrol.cloud_account import CloudAccount, CloudAccountError, SignedOut  # noqa: E402
from motioncontrol.fitness import FitnessStore  # noqa: E402
from motioncontrol.fitness_sync import FitnessSync  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def cloud(tmp_path_factory):
    folder = tmp_path_factory.mktemp("cloud")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "MC_DB_URL": f"sqlite+aiosqlite:///{(folder / 'cloud.db').as_posix()}",
           "MC_SECRET_KEY": "test-key-not-a-secret", "MC_ENV": "test", "MC_SITE_ORIGIN": base,
           "PYTHONIOENCODING": "utf-8"}
    subprocess.run([sys.executable, "-m", "alembic", "-c", "cloud/alembic.ini", "upgrade", "head"],
                   cwd=REPO, env=env, check=True, capture_output=True)
    made = subprocess.run([sys.executable, "-m", "cloud.tools.make_invite", "--note", "test"],
                          cwd=REPO, env=env, check=True, capture_output=True, text=True, encoding="utf-8")
    invite = made.stdout.splitlines()[1].strip()
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "cloud.app.main:app", "--host", "127.0.0.1",
                               "--port", str(port)], cwd=REPO, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/api/v1/health", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("云端没起来")
        yield base, invite
    finally:
        server.terminate()
        server.wait(timeout=10)


def _browser(base, invite):
    """网站那一半：注册（顺带登录）、点允许。带着 Cookie。"""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def post(path, body):
        request = urllib.request.Request(base + "/api/v1" + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(opener.open(request, timeout=5).read() or b"null")

    post("/auth/register", {"invite_code": invite, "email": "runner@example.com",
                            "password": "a-long-enough-passphrase", "display_name": "跑步的人"})
    return post


def _wait(condition, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


def _workout(store):
    pose = {name: {"x": x, "y": .5, "score": .99} for name, x in (("left_shoulder", .4), ("right_shoulder", .6),
                                                                 ("left_wrist", .3), ("right_wrist", .7))}
    store.control({"action": "profile", "profile": {"age": 33, "sex": "male", "weight_kg": 66}})
    store.control({"action": "start"})
    now = time.monotonic()
    for index in range(20):
        if index % 2 == 0:
            store.heart_rate({"bpm": 130})
        store.observe_pose(pose, now + index * .5, actions=("motion.march",))
    return store.control({"action": "finish"})["session_id"]


def test_two_pcs_on_one_account_share_the_record_and_logout_revokes(cloud, tmp_path):
    base, invite = cloud
    approve = _browser(base, invite)
    account = CloudAccount(tmp_path / "a" / "cloud_account.json", lambda: base, name="测试机 A")
    started = account.begin_login()
    assert started["state"] == "waiting" and started["verification_uri"].endswith(started["user_code"])
    approve("/device/approve", {"user_code": started["user_code"]})
    assert _wait(lambda: account.signed_in), account.status()
    assert account.status()["display_name"] == "跑步的人"

    first = FitnessStore(tmp_path / "a" / "fitness.json", background=False)
    session_id = _workout(first)
    sync = FitnessSync(first, account, background=False)
    assert sync.sync_once(), sync.error
    assert not first.cloud_pending()["sessions"], "传完就不再算没传过的"

    # 换了一台电脑：同一个账号（凭证文件拷过去，省得再走一遍浏览器）。
    other_dir = tmp_path / "b"
    other_dir.mkdir()
    (other_dir / "cloud_account.json").write_bytes((tmp_path / "a" / "cloud_account.json").read_bytes())
    second = FitnessStore(other_dir / "fitness.json", background=False)
    assert FitnessSync(second, CloudAccount(other_dir / "cloud_account.json", lambda: base),
                       background=False).sync_once()
    [session] = [item for item in second.history() if item["session_id"] == session_id]
    assert session["origin"] == "remote" and session["status"] == "finished"
    assert session["hr_avg"] == 130 and session["kcal_source"] == "heart_rate"
    assert second.profile["age"] == 33 and second.profile["sex"] == "male"
    assert second.current is None, "别的电脑上的锻炼不会变成这台电脑正在记的"

    account.logout()
    assert not account.signed_in and not (tmp_path / "a" / "cloud_account.json").exists()
    stale = CloudAccount(other_dir / "cloud_account.json", lambda: base)
    with pytest.raises(SignedOut):
        stale.request("GET", "/fitness")
    assert not stale.signed_in, "凭证被收回，本机也跟着退出"


def test_an_unreachable_cloud_is_a_plain_message(tmp_path):
    account = CloudAccount(tmp_path / "cloud_account.json", lambda: f"http://127.0.0.1:{_free_port()}")
    with pytest.raises(CloudAccountError, match="连不上云端"):
        account.begin_login()
    assert account.status()["state"] == "signed_out"
