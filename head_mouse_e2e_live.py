"""Live, human-only head-mouse direction check.

This diagnostic deliberately sends no synthetic pose and does not start a
service.  The operator must already have a real person in front of the real
    computer camera, with the 0.9.3 local service running and mouse output enabled.
The service then exercises the real ControlKernel -> OutputManager ->
SendInput path while this script records the Windows cursor.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from pathlib import Path
from urllib import request


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _cursor() -> tuple[int, int]:
    point = _Point()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        raise OSError("GetCursorPos failed")
    return int(point.x), int(point.y)


def _set_cursor(position: tuple[int, int]) -> None:
    if not ctypes.windll.user32.SetCursorPos(int(position[0]), int(position[1])):
        raise OSError("SetCursorPos failed")


def _get(base: str, route: str) -> dict:
    with request.urlopen(base + route, timeout=2.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base: str, route: str, body: dict) -> dict:
    raw = json.dumps(body).encode("utf-8")
    req = request.Request(
        base + route,
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=2.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_operator(prompt: str) -> tuple[int, int]:
    input(prompt)
    time.sleep(0.25)
    return _cursor()


def main() -> int:
    parser = argparse.ArgumentParser(description="真人 + 真实摄像头的头控鼠标四向诊断")
    parser.add_argument("--port", type=int, default=8766,
                        help="本机管理面端口；/api/* 自 2.0 起只在 127.0.0.1:8766")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("This diagnostic requires real Windows SendInput")

    base = f"http://127.0.0.1:{args.port}"
    kernel = _get(base, "/api/kernel/status")
    output = _get(base, "/api/output-status")
    if kernel.get("version") != "0.9.3":
        raise SystemExit(f"wrong service version: {kernel.get('version')!r}")
    camera = (kernel.get("camera") or {})
    if not camera.get("running") or kernel.get("body_mode") != "computer":
        raise SystemExit("real computer camera source is not running")
    if (kernel.get("kernel") or {}).get("active_body_source") != "computer_camera":
        raise SystemExit("no current real camera pose source")
    if output.get("mode") != "mouse" or not output.get("enabled"):
        raise SystemExit("enable mouse output in the local service before starting")

    original = _cursor()
    results: list[dict] = []
    directions = (
        ("left", "Turn your head LEFT, then press Enter."),
        ("right", "Turn your head RIGHT, then press Enter."),
        ("up", "Look UP, then press Enter."),
        ("down", "Look DOWN, then press Enter."),
    )
    expected = {"left": "x<0", "right": "x>0", "up": "y<0", "down": "y>0"}
    try:
        print("Use only a real person and the live computer camera; no pose is synthesized.")
        for name, action_prompt in directions:
            center = _wait_for_operator(f"Return to neutral for {name}, then press Enter: ")
            after = _wait_for_operator(action_prompt + " ")
            dx, dy = after[0] - center[0], after[1] - center[1]
            result = {"direction": name, "before": center, "after": after, "dx": dx, "dy": dy, "expected": expected[name]}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False))
    finally:
        try:
            _post(base, "/api/output/stop", {})
        finally:
            _set_cursor(original)
    print(json.dumps({"results": results, "output_disabled": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
