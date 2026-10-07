"""微软 Kinect 彩色和原厂深度骨骼采集；沿用现有图像人体识别。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import threading
import time

import numpy as np

from motioncontrol.user_paths import user_data_root

PREFIX = "kinect2:"


def _runtime_dll():
    if os.name != "nt":
        return None
    gac = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/assembly/GAC_64/Microsoft.Kinect"
    return next(iter(sorted(gac.glob("*/Microsoft.Kinect.dll"), reverse=True)), None)


def bridge_executable():
    runtime = _runtime_dll()
    if runtime is None:
        raise RuntimeError("未安装微软 Kinect 原厂运行库，请先安装后再连接。")
    source = Path(__file__).resolve().parents[1] / "tools/kinect_camera/KinectCamera.cs"
    digest = hashlib.sha256(source.read_bytes() + str(runtime).encode()).hexdigest()[:16]
    root = user_data_root() / "cache/kinect" / digest
    executable = root / "KinectCamera.exe"
    if executable.is_file():
        return executable
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise RuntimeError("未找到 Windows 自带的 .NET 编译工具，无法准备 Kinect 采集程序。")
    root.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([str(compiler), "/nologo", "/target:exe", "/platform:x64",
                             "/r:System.Web.Extensions.dll", "/r:" + str(runtime),
                             "/out:" + str(executable), str(source)],
                            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Kinect 采集程序准备失败：" + result.stdout.decode("utf-8", errors="replace"))
    # The managed assembly is loaded from the installed runtime, not a developer checkout.
    import shutil
    shutil.copy2(runtime, root / "Microsoft.Kinect.dll")
    return executable


def list_devices():
    if _runtime_dll() is None:
        return []
    result = subprocess.run([str(bridge_executable()), "--list"], capture_output=True,
                            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        return []
    return json.loads(result.stdout.decode("utf-8-sig"))


def rotate_point(x, y, rotation):
    if rotation == "cw":
        return 1 - y, x
    if rotation == "ccw":
        return y, 1 - x
    if rotation == "180":
        return 1 - x, 1 - y
    return x, y


def matching_depth_body(metadata, pose_map, rotation="none"):
    """只接纳与当前二维人物匹配的可靠原厂关节；不采用推断关节或旧帧。"""
    if not metadata or not metadata.get("depth_valid") or not pose_map:
        return None
    names = ("left_hip", "right_hip", "left_shoulder", "right_shoulder")
    if any(not pose_map.get(name) or pose_map[name].get("score", 0) < .6 for name in names):
        return None
    candidates = []
    for body in metadata.get("bodies", []):
        points = body.get("points", {})
        if any(name not in points for name in names):
            continue
        errors = []
        for name in names:
            point = points[name]
            x, y = rotate_point(point["color_x"], point["color_y"], rotation)
            observed = pose_map[name]
            errors.append((x - observed["x"]) ** 2 + (y - observed["y"]) ** 2)
        error = sum(errors) / len(errors)
        if error < .08 ** 2 and max(errors) < .15 ** 2:
            candidates.append((error, body))
    if not candidates:
        return None
    # Ambiguous overlapping people are not a reliable depth match.
    candidates.sort(key=lambda item: item[0])
    if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < .01 ** 2:
        return None
    result = dict(candidates[0][1])
    result["floor"] = metadata.get("floor")
    return result


class KinectCapture:
    """与普通摄像头相同的 read/release 生命周期，额外保留同帧深度信息。"""
    def __init__(self, device_id, *, depth_enabled=True):
        args = [str(bridge_executable())]
        if not depth_enabled:
            args.append("--no-depth")
        self.device_id = device_id
        self.metadata = None
        self.last_error = ""
        self._latest = None
        self._sequence = self._read_sequence = 0
        self._closed = False
        self._condition = threading.Condition()
        self._started = time.monotonic()
        self._process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="kinect-pipe")
        self._reader.start()

    @staticmethod
    def _exact(stream, size):
        parts = bytearray()
        while len(parts) < size:
            block = stream.read(size - len(parts))
            if not block:
                raise EOFError
            parts.extend(block)
        return bytes(parts)

    def _read_loop(self):
        try:
            while not self._closed:
                lengths = self._exact(self._process.stdout, 8)
                meta_size, image_size = struct.unpack("<II", lengths)
                if not 0 < meta_size <= 100_000 or image_size not in (0, 640 * 360 * 3):
                    raise ValueError("Kinect 采集帧格式无效")
                metadata = json.loads(self._exact(self._process.stdout, meta_size))
                if image_size:
                    pixels = self._exact(self._process.stdout, image_size)
                    if self.device_id != "kinect2:default" and metadata.get("device_id") != self.device_id:
                        raise ValueError("Kinect 设备已改变，请重新扫描选择摄像头")
                    metadata["sample_at"] = metadata["qpc"] + time.monotonic() - time.perf_counter()
                    frame = np.frombuffer(pixels, dtype=np.uint8).reshape(360, 640, 3)
                    with self._condition:
                        self._latest = frame, metadata
                        self._sequence += 1
                        self._condition.notify_all()
                elif metadata.get("gap_s", 0) >= 5 and self._sequence:
                    raise RuntimeError("微软 Kinect 已停止供帧，请检查连接后重新连接")
                elif time.monotonic() - self._started >= 20 and not self._sequence:
                    raise RuntimeError("微软 Kinect 未收到画面，请检查电源、USB 接口或设备占用")
        except EOFError:
            if not self._closed:
                self.last_error = self._process.stderr.read().decode("utf-8", errors="replace").strip() or "Kinect 采集程序已退出"
        except Exception as exc:
            self.last_error = str(exc)
            if self._process.poll() is None:
                self._process.terminate()
        finally:
            with self._condition:
                self._condition.notify_all()

    def read(self):
        with self._condition:
            deadline = time.monotonic() + 22
            while (not self._closed and not self.last_error and self._reader.is_alive()
                   and self._sequence <= self._read_sequence):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.last_error = "微软 Kinect 等待画面超时"
                    break
                self._condition.wait(min(remaining, .2))
            if self._closed or self.last_error or self._sequence <= self._read_sequence:
                return False, None
            self._read_sequence = self._sequence
            frame, self.metadata = self._latest
            return True, frame

    def interrupt(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._process.poll() is None:
            self._process.terminate()
    def release(self):
        self.interrupt()
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=3)
        self._reader.join(timeout=3)
        self._process.stdout.close()
        self._process.stderr.close()
