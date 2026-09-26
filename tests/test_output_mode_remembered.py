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
print("SPEED_AT_START=" + json.dumps({key: getattr(server.OUTPUT, key) for key in server._OUTPUT_SPEED_FIELDS}))
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
    print("SPEED_AFTER=" + json.dumps({key: getattr(server.OUTPUT, key) for key in server._OUTPUT_SPEED_FIELDS}))
sys.stdout.flush()
server.os._exit(0)
"""


def run(user_dir: Path, body: dict | None = None) -> dict:
    env = {**os.environ, "MOTIONCONTROL_USER_DIR": str(user_dir), "PYTHONIOENCODING": "utf-8"}
    args = [sys.executable, "-c", CHILD] + ([json.dumps(body)] if body is not None else [])
    done = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    lines = dict(line.split("=", 1) for line in done.stdout.splitlines() if line.startswith(("MODE_", "SPEED_")))
    lines = {key: json.loads(value) if key.startswith("SPEED_") else value for key, value in lines.items()}
    assert "MODE_AT_START" in lines, done.stdout + done.stderr
    return lines


def test_the_look_output_choice_survives_a_restart(isolated_user_data):
    assert run(isolated_user_data)["MODE_AT_START"] == "mouse", "第一次打开还是鼠标"
    assert run(isolated_user_data, {"mode": "gamepad"})["MODE_AFTER"] == "gamepad"
    assert run(isolated_user_data)["MODE_AT_START"] == "gamepad", "重启后还是上次选的 Xbox 右摇杆"
    assert run(isolated_user_data, {"mode": "mouse"})["MODE_AFTER"] == "mouse"
    assert run(isolated_user_data)["MODE_AT_START"] == "mouse", "改回鼠标也记得住"


def test_output_speed_survives_restart_and_mode_changes(isolated_user_data):
    speed = {"mouse_speed_x": 780., "mouse_speed_y": 585., "gamepad_gain": 1.3}
    assert run(isolated_user_data, speed)["SPEED_AFTER"] == speed
    assert run(isolated_user_data, {"mode": "gamepad"})["SPEED_AT_START"] == speed
    changed = {"mouse_speed_x": 540., "mouse_speed_y": 405., "gamepad_gain": .9}
    assert run(isolated_user_data, changed)["SPEED_AFTER"] == changed
    assert run(isolated_user_data, {"mode": "mouse"})["SPEED_AT_START"] == changed
    assert run(isolated_user_data)["SPEED_AT_START"] == changed


def test_corrupt_saved_speed_does_not_break_startup_or_other_preferences(isolated_user_data):
    from motioncontrol.user_paths import user_path

    path = user_path("general_settings")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "output_speed": {"mouse_speed_x": 840., "mouse_speed_y": float('inf'), "gamepad_gain": "坏值"},
        "audio_source": "phone", "trigger_recording": {
            "enabled": False, "triggers": ["zone.leftFoot"], "pre_s": 1., "post_s": 1.},
    }), encoding="utf-8")
    result = run(isolated_user_data, {"gamepad_gain": 1.4})
    assert result["SPEED_AT_START"] == {"mouse_speed_x": 840., "mouse_speed_y": 450., "gamepad_gain": 1.6}
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["output_speed"]["gamepad_gain"] == 1.4
    assert saved["audio_source"] == "phone"
    assert saved["trigger_recording"] == {
        "enabled": False, "triggers": ["zone.leftFoot"], "pre_s": 1., "post_s": 1.}
