"""微软 Kinect 彩色和原厂深度骨骼采集；沿用现有图像人体识别。"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import threading
import time

import numpy as np

from motioncontrol import owned_process
from motioncontrol.user_paths import user_data_root

PREFIX = "kinect2:"
STARTUP_TIMEOUT_S = 45


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
    result = owned_process.run([str(compiler), "/nologo", "/target:exe", "/platform:x64",
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
    result = owned_process.run([str(bridge_executable()), "--list"], capture_output=True,
                            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        return []
    return json.loads(result.stdout.decode("utf-8-sig"))


SERVICE_NAME = "kinectservice.exe"
BOOST_ERRORS = {2: "未找到 Kinect 原厂服务", 3: "没有取得调整原厂服务的权限",
                4: "显卡调度优先级未被系统接受"}
_service_pid = None
# 辅助程序退出码对应实际生效的显卡调度级别；只有管理员能读回，所以记下本次结果供诊断。
GPU_LEVELS = {0: "realtime", 10: "high"}
last_gpu_priority = None


def _service_process():
    global _service_pid
    import psutil
    if _service_pid:
        try:
            process = psutil.Process(_service_pid)
            if process.name().lower() == SERVICE_NAME:
                return process
        except psutil.Error:
            pass
    for process in psutil.process_iter(["name"]):
        if (process.info["name"] or "").lower() == SERVICE_NAME:
            _service_pid = process.pid
            return process
    return None


def service_pid():
    try:
        process = _service_process()
    except Exception:
        return None
    return None if process is None else process.pid


def service_boosted():
    """原厂服务是否已按授权调高；服务或电脑重启后系统恢复默认。读取不需要管理员权限。"""
    try:
        import psutil
        process = _service_process()
        # 提高时处理器同时调到“高”，以此判断；旧版只调到“高于普通”的视为未提高，会重新申请。
        return None if process is None else process.nice() in (
            psutil.HIGH_PRIORITY_CLASS, psutil.REALTIME_PRIORITY_CLASS)
    except Exception:
        return None


def _run_elevated(executable, arguments, timeout_s):
    """弹出 Windows 管理员确认，由用户决定；返回退出码。后台发起时确认窗口可能只在任务栏闪烁。"""
    import ctypes
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = (("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE))

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(ShellExecuteInfo),)
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    # 0x40 保留进程句柄以读取退出码；0x100 等启动完成再返回。窗口隐藏。
    info = ShellExecuteInfo(cbSize=ctypes.sizeof(ShellExecuteInfo), fMask=0x40 | 0x100, lpVerb="runas",
                            lpFile=str(executable), lpParameters=arguments, nShow=0)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:  # ERROR_CANCELLED：用户在确认窗口点了“否”。
            raise PermissionError("已取消管理员授权，原厂服务优先级未改变")
        raise ctypes.WinError(error)
    try:
        if kernel32.WaitForSingleObject(info.hProcess, int(timeout_s * 1000)) != 0:
            raise TimeoutError("等待调整原厂服务超时")
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel32.CloseHandle(info.hProcess)


def _change_service(argument, timeout_s):
    """需要管理员权限：未以管理员运行时先征得用户确认。"""
    global last_gpu_priority
    from motioncontrol.game_launch import is_administrator
    executable = bridge_executable()
    if is_administrator():
        code = owned_process.run([str(executable), argument], capture_output=True,
                                 timeout=timeout_s, creationflags=subprocess.CREATE_NO_WINDOW).returncode
    else:
        code = _run_elevated(executable, argument, timeout_s)
    raising = argument == "--boost-service"
    if code not in (GPU_LEVELS if raising else (0,)):
        raise RuntimeError(BOOST_ERRORS.get(code, f"调整原厂服务失败（{code}）"))
    last_gpu_priority = GPU_LEVELS[code] if raising else None
    return {"boosted": bool(service_boosted()), "gpu_priority": last_gpu_priority}


def boost_service(timeout_s=120):
    """调高原厂服务的处理器与显卡调度。"""
    return _change_service("--boost-service", timeout_s)


def restore_service(timeout_s=120):
    """恢复原厂服务的默认处理器与显卡调度。"""
    return _change_service("--restore-service", timeout_s)


def rotate_point(x, y, rotation):
    if rotation == "cw":
        return 1 - y, x
    if rotation == "ccw":
        return y, 1 - x
    if rotation == "180":
        return 1 - x, 1 - y
    return x, y


def _valid_depth_point(point):
    try:
        return (.6 <= float(point.get("score", 0)) <= 1 and float(point["z"]) > 0
                and all(math.isfinite(float(point[key])) for key in ("x", "y", "z", "color_x", "color_y")))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False


def matching_depth_body(metadata, pose_map, rotation="none"):
    """只接纳与当前二维人物匹配的可靠原厂关节；不采用推断关节或旧帧。"""
    if not metadata or not metadata.get("depth_valid") or not pose_map:
        return None
    names = tuple(name for name in ("left_hip", "right_hip", "left_shoulder", "right_shoulder")
                  if pose_map.get(name) and pose_map[name].get("score", 0) >= .6
                  and all(math.isfinite(pose_map[name].get(key, math.nan)) for key in ("x", "y")))
    # 坐着或下半身出画时仍可用双肩匹配；至少两个可靠身体点，不能只凭一个点接纳另一人。
    if len(names) < 2:
        return None
    candidates = []
    for body in metadata.get("bodies", []):
        points = body.get("points", {})
        mirrored = bool(metadata.get("image_mirrored"))
        matches = []
        # 镜像画面重新识别后，按肩髋位置确认图像与原厂骨骼的左右命名。
        # 每个人只保留一个对应关系，不能把同一个人的两种命名当成两个人。
        for swapped in ((False, True) if mirrored else (False,)):
            matched = {("right_" + name[5:] if name.startswith("left_") else
                        "left_" + name[6:] if name.startswith("right_") else name): point
                       for name, point in points.items()} if swapped else points
            anchors = tuple(name for name in names if _valid_depth_point(matched.get(name)))
            if len(anchors) < 2:
                continue
            errors = []
            for name in anchors:
                point = matched[name]
                x, y = rotate_point(point["color_x"], point["color_y"], rotation)
                if mirrored:
                    x = 1 - x
                observed = pose_map[name]
                errors.append((x - observed["x"]) ** 2 + (y - observed["y"]) ** 2)
            error = sum(errors) / len(errors)
            if error < .08 ** 2 and max(errors) < .15 ** 2:
                states = body.get("hand_states") or {}
                if swapped:
                    states = {"left": states.get("right"), "right": states.get("left")}
                matches.append((error, {**body, "points": matched, "hand_states": states}))
        if matches:
            candidates.append(min(matches, key=lambda match: match[0]))
    if not candidates:
        return None
    # Ambiguous overlapping people are not a reliable depth match.
    candidates.sort(key=lambda item: item[0])
    if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < .01 ** 2:
        return None
    result = dict(candidates[0][1])
    result["floor"] = metadata.get("floor")
    # 学习与录制必须能核对这是同一彩色帧匹配到的人，不能把旧骨骼当作监督真值。
    result.update(matched_to_model=True, match_error=math.sqrt(candidates[0][0]),
                  body_valid=metadata.get("body_valid") is True)
    for key in ("sample_at", "body_age_ms", "sensor_seconds", "device_id", "body_frames"):
        if key in metadata:
            result[key] = metadata[key]
    return result


# 只替换解剖位置明确对应的点。原厂头中心不是鼻子、手尖不是食指、足部不是脚跟。
DEPTH_POSE_NAMES = frozenset(side + "_" + joint for side in ("left", "right")
                           for joint in ("shoulder", "elbow", "wrist", "thumb", "hip", "knee", "ankle"))


def merge_depth_pose(pose_map, body, rotation="none", *, mirrored=False):
    """把同一人物的可靠原厂点投影到当前画面，保留完整的 33 点结构。"""
    if not pose_map or not body:
        return pose_map
    result = dict(pose_map)
    for name in DEPTH_POSE_NAMES.intersection(pose_map):
        point = body.get("points", {}).get(name)
        if not _valid_depth_point(point):
            continue
        x, y = rotate_point(float(point["color_x"]), float(point["color_y"]), rotation)
        if mirrored:
            x = 1 - x
        # 图像 z 仍是模型的相对单位；原厂以米计的三维坐标另存，不能直接混入归一化图像坐标。
        result[name] = {**pose_map[name], "x": x, "y": y, "score": float(point["score"]),
                        "position_source": "kinect",
                        "camera_space": {key: float(point[key]) for key in ("x", "y", "z")}}
    return result


def depth_hand_states(body):
    """只使用同一人物可靠的张手/握拳状态；未知、推断或手腕不可见时回退原识别。"""
    if not body:
        return {}
    result = {}
    for side, state in (body.get("hand_states") or {}).items():
        wrist = body.get("points", {}).get(side + "_wrist")
        if side not in ("left", "right") or not _valid_depth_point(wrist):
            continue
        if (not isinstance(state, dict) or state.get("state") not in ("open", "closed")
                or not isinstance(state.get("score"), (int, float)) or not .6 <= state["score"] <= 1):
            continue
        result[side] = dict(state)
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
        self._release_lock = threading.Lock()
        self._released = False
        self._condition = threading.Condition()
        self._started = time.monotonic()
        self._process = owned_process.popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="kinect-pipe")
        try:
            self._reader.start()
        except Exception:
            owned_process.terminate_and_wait(self._process)
            self._process.stdout.close()
            self._process.stderr.close()
            raise

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
                    reported_id = metadata.get("device_id")
                    if reported_id in (None, "", "kinect2:", "kinect2:default"):
                        # 原厂接口刚重连时可能先供图、后给序列号；这类帧先不接纳。
                        if not self._sequence and time.monotonic() - self._started >= STARTUP_TIMEOUT_S:
                            raise RuntimeError("微软 Kinect 尚未准备好设备编号，请稍后重试")
                        continue
                    if self.device_id != "kinect2:default" and reported_id != self.device_id:
                        raise ValueError(f"Kinect 设备已改变（当前 {reported_id}，选择 {self.device_id}），请重新扫描选择摄像头")
                    metadata["sample_at"] = metadata["qpc"] + time.monotonic() - time.perf_counter()
                    frame = np.frombuffer(pixels, dtype=np.uint8).reshape(360, 640, 3)
                    with self._condition:
                        self._latest = frame, metadata
                        self._sequence += 1
                        self._condition.notify_all()
                elif metadata.get("gap_s", 0) >= 5 and self._sequence:
                    raise RuntimeError("微软 Kinect 已停止供帧，请检查连接后重新连接")
                elif time.monotonic() - self._started >= STARTUP_TIMEOUT_S and not self._sequence:
                    raise RuntimeError("微软 Kinect 未收到画面，请检查电源、USB 接口或设备占用")
        except EOFError:
            if not self._closed:
                owned_process.terminate_and_wait(self._process)
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
            deadline = time.monotonic() + (STARTUP_TIMEOUT_S + 2 if not self._sequence else 7)
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
        with self._release_lock:
            if self._released:
                return
            self.interrupt()
            owned_process.terminate_and_wait(self._process)
            self._reader.join(timeout=3)
            self._process.stdout.close()
            self._process.stderr.close()
            self._released = True
