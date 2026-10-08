"""Windows 原生视频设备名称；只枚举，不开启任何摄像头。"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
from motioncontrol.user_paths import user_data_root

_lock = threading.Lock()
_devices = None

def list_devices(*, refresh=False):
    global _devices
    if os.name != "nt":
        return []
    with _lock:
        if _devices is None or refresh:
            source = Path(__file__).resolve().parents[1] / "tools/camera_devices/CameraDevices.cs"
            digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
            cache = user_data_root() / "cache/cameras" / digest
            executable = cache / "CameraDevices.exe"
            if not executable.is_file():
                compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
                cache.mkdir(parents=True, exist_ok=True)
                result = subprocess.run([str(compiler), "/nologo", "/target:exe", "/platform:x64",
                                         "/r:System.Web.Extensions.dll", "/out:" + str(executable), str(source)],
                                        capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    raise RuntimeError("无法读取摄像头名称：" + result.stdout.decode("utf-8", errors="replace"))
            result = subprocess.run([str(executable)], capture_output=True, timeout=8,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise RuntimeError("无法读取摄像头名称：" + result.stderr.decode("utf-8", errors="replace"))
            _devices = []
            labels = {"Integrated Camera": "内置摄像头", "Kinect V2 Video Sensor": "微软 Kinect 普通视频接口",
                      "WebcastMate VirtualCamera": "直播伴侣虚拟摄像头", "OBS Virtual Camera": "OBS 虚拟摄像头"}
            for item in json.loads(result.stdout.decode("utf-8-sig")):
                raw = str(item.get("name") or "")
                path = str(item.get("device_path") or "").lower()
                _devices.append({**item, "name": labels.get(raw, raw or "摄像头 " + str(item["index"])),
                                 "virtual": str(item.get("moniker") or "").startswith(("@device:sw:", "@device_sw_")),
                                 "kinect_v2": "kinectmfmediasource" in path or raw == "Kinect V2 Video Sensor"})
        return [dict(item) for item in _devices]
