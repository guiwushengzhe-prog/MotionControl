"""录制设置、人物输出租约以及逐块保存的视频文件。"""
from __future__ import annotations

import copy
import json
import math
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from motioncontrol.config_transaction import atomic_bytes
from motioncontrol.studio_portrait import decode_avatar

DEFAULT_CONFIG = {"source": "computer", "background": "original", "face": "original",
                  "avatar_data_url": "", "layout": "landscape", "position": "bottom-right",
                  "size": 30, "capture": "window", "system_audio": True, "microphone": False}
MAX_CHUNK_BYTES = 16 * 1024 * 1024


class StudioService:
    def __init__(self, portrait, bridge, root: Path, *, clock=time.monotonic, background=True):
        self.portrait, self.bridge, self.clock = portrait, bridge, clock
        self.root = Path(root)
        self.config_path = self.root / "studio.json"
        self.folder = self.root / "recordings" / "studio"
        self.lock = threading.RLock()
        self._leases = {}
        self._recordings = {}
        self._recording_seen = {}
        self._stop = threading.Event()
        self._thread = None
        self.error = ""
        self.config = dict(DEFAULT_CONFIG)
        try:
            saved = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.config = self._validated(saved)
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, OSError):
            self.error = "录制设置读取失败，使用默认设置"
        self.portrait.update(self.config)
        if background:
            self._thread = threading.Thread(target=self._sweep_loop, name="motion-studio-leases", daemon=True)
            self._thread.start()

    def _validated(self, patch):
        if not isinstance(patch, dict):
            raise ValueError("录制设置必须是一组设置")
        config = dict(self.config)
        choices = {"source": {"computer", "phone"}, "background": {"original", "green", "transparent"},
                   "face": {"original", "mask", "avatar"}, "layout": {"landscape", "portrait"},
                   "position": {"bottom-right", "bottom-left", "top-right", "top-left"},
                   "capture": {"window", "monitor"}}
        for key, allowed in choices.items():
            if key in patch:
                if patch[key] not in allowed:
                    raise ValueError("录制选项无效：" + key)
                config[key] = patch[key]
        for key in ("microphone", "system_audio"):
            if key in patch:
                if type(patch[key]) is not bool:
                    raise ValueError("声音选项必须为开启或关闭")
                config[key] = patch[key]
        if "size" in patch:
            size = patch["size"]
            if isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size) or not 15 <= size <= 65:
                raise ValueError("人物大小必须在15%到65%之间")
            config["size"] = int(size)
        if "avatar_data_url" in patch:
            avatar = patch["avatar_data_url"]
            if not isinstance(avatar, str) or len(avatar) > 2 * 1024 * 1024:
                raise ValueError("头像图片过大，请选择较小的图片")
            if avatar:
                decode_avatar(avatar)  # 先完整校验，保存失败时不改变正在使用的遮脸设置。
            config["avatar_data_url"] = avatar
        return config

    def update(self, patch):
        with self.lock:
            config = self._validated(patch)
            atomic_bytes(self.config_path, json.dumps(config, ensure_ascii=False).encode("utf-8"))
            self.portrait.update(config)
            self.config = config
            self._sync_demand()
            return self.state()

    def demand(self, enabled, client_id="main"):
        if type(enabled) is not bool or not isinstance(client_id, str) or not 1 <= len(client_id) <= 100:
            raise ValueError("人物输出请求无效")
        with self.lock:
            if enabled:
                self._leases[client_id] = self.clock() + 30
            else:
                self._leases.pop(client_id, None)
            self._sync_demand()
            return self.state()

    def _sync_demand(self):
        now = self.clock()
        self._leases = {key: deadline for key, deadline in self._leases.items() if deadline > now}
        enabled = bool(self._leases)
        self.portrait.set_enabled(enabled)
        self.bridge.set_studio_video(enabled and self.config["source"] == "phone")
        return enabled

    def state(self):
        with self.lock:
            self._sync_demand()
            portrait = self.portrait.status()
            return {"config": copy.deepcopy(self.config), "frame_available": bool(portrait.get("ready")),
                    "status": portrait, "message": portrait.get("reason") or self.error,
                    "privacy_hidden": not portrait.get("ready", False) and self.config["face"] != "original",
                    "recordings": [dict(item) for item in self._recordings.values()], "error": self.error}

    def frame_png(self):
        return self.frame_snapshot_png()[0]

    def frame_snapshot_png(self):
        with self.lock:
            if not self._sync_demand():
                return None, self.portrait.status().get("revision", 0)
        return self.portrait.frame_snapshot_png()

    def _sweep(self):
        with self.lock:
            self._sync_demand()
            now = self.clock()
            for ident, last_seen in self._recording_seen.items():
                item = self._recordings[ident]
                if not item["finished"] and now - last_seen > 120:
                    item["finished"] = True
                    item["interrupted"] = True

    def _sweep_loop(self):
        while not self._stop.wait(2):
            self._sweep()

    def start_recording(self, body):
        mime = body.get("mime_type", "video/webm")
        if not isinstance(mime, str) or mime.split(";", 1)[0] not in {"video/webm", "video/mp4"}:
            raise ValueError("不支持这种视频格式")
        with self.lock:
            if any(not item["finished"] for item in self._recordings.values()):
                raise ValueError("已有录像正在保存，请先停止当前录制")
            self.folder.mkdir(parents=True, exist_ok=True)
            ident = uuid.uuid4().hex
            extension = ".mp4" if mime.startswith("video/mp4") else ".webm"
            name = "MotionControl-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + ident[:6] + extension
            path = self.folder / name
            path.touch(exist_ok=False)
            item = {"id": ident, "file_name": name, "path": str(path), "bytes": 0,
                    "mime_type": mime, "finished": False, "interrupted": False}
            self._recordings[ident] = item
            self._recording_seen[ident] = self.clock()
            return dict(item)

    def append_recording(self, ident, chunk):
        if not chunk or len(chunk) > MAX_CHUNK_BYTES:
            raise ValueError("录像片段为空或过大")
        with self.lock:
            item = self._recordings.get(ident)
            if item is None or item["finished"]:
                raise ValueError("录像已经结束，请重新开始录制")
            if not item["bytes"] and item["mime_type"].startswith("video/webm") and not chunk.startswith(b"\x1a\x45\xdf\xa3"):
                raise ValueError("录像首段不是有效的视频文件")
            with Path(item["path"]).open("ab") as stream:
                stream.write(chunk)
            item["bytes"] += len(chunk)
            self._recording_seen[ident] = self.clock()
            return {"id": ident, "bytes": item["bytes"]}

    def finish_recording(self, ident, *, interrupted=False):
        with self.lock:
            item = self._recordings.get(ident)
            if item is None:
                raise ValueError("找不到这段录像")
            item["finished"] = True
            item["interrupted"] = bool(interrupted)
            return dict(item)

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self.lock:
            self._leases.clear()
            self._sync_demand()
            for item in self._recordings.values():
                if not item["finished"]:
                    item["finished"] = True
                    item["interrupted"] = True
        self.portrait.close()
