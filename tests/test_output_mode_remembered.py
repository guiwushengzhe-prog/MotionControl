"""视角输出（鼠标 / Xbox 右摇杆）记住上一次的选择，重启后不回到默认的鼠标。

每一步都在新进程里 import server：这件事坏就坏在「重启」那一下，同一个进程里测
不出来。改设置走真的 HTTP 接口，和页面上点下拉框是同一条路。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHILD = r"""
import json, sys, threading, urllib.request
from http.server import ThreadingHTTPServer
import server

print("MODE_AT_START=" + server.OUTPUT.mode)
body = json.loads(sys.argv[1]) if len(sys.argv) > 1 else None
if body is not None:
    http = ThreadingHTTPServer(("127.0.0.1", 0), server.AdminHandler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    request = urllib.request.Request(
        f"http://127.0.0.1:{http.server_address[1]}/api/output/config",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(request, timeout=10).read()
    except urllib.error.HTTPError:
        pass  # 这台机器没有虚拟手柄驱动时选手柄会报错，模式照样切过去
    http.shutdown()
    print("MODE_AFTER=" + server.OUTPUT.mode)
sys.stdout.flush()
server.os._exit(0)
"""


def run(user_dir: Path, body: dict | None = None) -> dict:
    env = {**os.environ, "MOTIONCONTROL_USER_DIR": str(user_dir), "PYTHONIOENCODING": "utf-8"}
    args = [sys.executable, "-c", CHILD] + ([json.dumps(body)] if body is not None else [])
    done = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    lines = dict(line.split("=", 1) for line in done.stdout.splitlines() if line.startswith("MODE_"))
    assert "MODE_AT_START" in lines, done.stdout + done.stderr
    return lines


def test_the_look_output_choice_survives_a_restart(isolated_user_data):
    assert run(isolated_user_data)["MODE_AT_START"] == "mouse", "第一次打开还是鼠标"
    assert run(isolated_user_data, {"mode": "gamepad"})["MODE_AFTER"] == "gamepad"
    assert run(isolated_user_data)["MODE_AT_START"] == "gamepad", "重启后还是上次选的 Xbox 右摇杆"
    assert run(isolated_user_data, {"mode": "mouse"})["MODE_AFTER"] == "mouse"
    assert run(isolated_user_data)["MODE_AT_START"] == "mouse", "改回鼠标也记得住"
