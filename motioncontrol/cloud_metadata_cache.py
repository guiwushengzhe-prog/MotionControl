"""Small public metadata snapshots; downloads and install validation bypass this cache."""

from __future__ import annotations

import copy
import json
import math
import os
import queue
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from motioncontrol.cloud_client import CloudError

_KINDS = {"health", "browse", "pose_library"}
_MAX_DISK_BYTES = 4 << 20


def _select(value, fields):
    return {key: copy.deepcopy(value[key]) for key in fields if key in value}


def _public_value(kind: str, value):
    """Keep display metadata, never configuration documents, signatures or tokens."""
    if kind == "health":
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise CloudError("云端服务暂时不可用")
        return _select(value, ("ok", "schema", "games", "profiles"))
    if kind == "browse":
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise CloudError("云端返回的列表格式不对")
        result = []
        for item in value:
            public = _select(item, ("id", "title", "doc_type", "owner_name", "game_id", "game_name"))
            version = item.get("current_version")
            public["current_version"] = (_select(version, ("id", "revision_no", "canonical_sha256",
                                                          "schema_version", "created_at"))
                                         if isinstance(version, dict) else None)
            result.append(public)
        return result
    if not isinstance(value, dict) or not isinstance(value.get("actions"), list):
        raise CloudError("云端返回的动作库格式不对")
    result = _select(value, ("engine", "rating_names", "body_part_names"))
    result["actions"] = [_select(item, ("id", "trigger", "group", "name", "how", "ratings", "body_parts",
                                       "passes_zones", "source", "revision", "demo", "sha256"))
                         for item in value["actions"] if isinstance(item, dict) and item.get("id")]
    return result


@dataclass
class _Entry:
    kind: str
    data: object = None
    updated_at: float = 0.0
    checked_at: float = 0.0
    error: str = ""
    next_retry: float = 0.0
    ready: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self):
        self.ready.set()


class PublicMetadataCache:
    def __init__(self, path: Path | None = None, *, ttl_s: float = 60.0, retry_s: float = 15.0,
                 max_entries: int = 32, wait_s: float = 8.0, clock=time.time, monotonic=time.monotonic):
        self.path = Path(path) if path is not None else None
        self.ttl_s, self.retry_s, self.wait_s = ttl_s, retry_s, wait_s
        self.max_entries = max(1, max_entries)
        self.clock, self.monotonic = clock, monotonic
        self._lock = threading.RLock()
        self._disk_lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._pending = queue.Queue(maxsize=self.max_entries)
        self._workers: list[threading.Thread] = []
        self._stop = threading.Event()
        self._closed = False
        self._load()

    @staticmethod
    def _key(endpoint: str, kind: str, parameters: dict) -> str:
        if kind not in _KINDS:
            raise ValueError("Only public list metadata can be cached")
        canonical = json.dumps([endpoint.rstrip("/"), kind, parameters], sort_keys=True, separators=(",", ":"))
        # No endpoint credentials or query strings are stored on disk.
        return sha256(canonical.encode("utf-8")).hexdigest()

    def _load(self):
        if self.path is None:
            return
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(_MAX_DISK_BYTES + 1)
            if len(raw) > _MAX_DISK_BYTES:
                return
            saved = json.loads(raw)
            if saved.get("schema") != "motioncontrol.public_metadata_cache.v1":
                return
            for key, item in list(saved["entries"].items())[-self.max_entries:]:
                kind, updated = item["kind"], float(item["updated_at"])
                if kind not in _KINDS or not math.isfinite(updated) or updated <= 0 or len(key) != 64:
                    continue
                self._entries[key] = _Entry(kind, _public_value(kind, item["data"]), updated, updated)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, CloudError):
            # Metadata is optional; damaged cache files never stop local control.
            return

    def _persist(self):
        if self.path is None:
            return
        temporary = None
        try:
            with self._disk_lock:
                with self._lock:
                    values = {key: {"kind": entry.kind, "data": entry.data, "updated_at": entry.updated_at}
                              for key, entry in self._entries.items() if entry.data is not None}
                    payload = json.dumps({"schema": "motioncontrol.public_metadata_cache.v1", "entries": values},
                                         ensure_ascii=False, allow_nan=False).encode("utf-8")
                if len(payload) > _MAX_DISK_BYTES:
                    return
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".cloud-cache-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(payload)
                os.replace(temporary, self.path)
        except (OSError, ValueError, TypeError):
            pass  # A full/locked disk can only disable persistence, not browsing.
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _refresh(self, entry: _Entry, loader):
        try:
            value = _public_value(entry.kind, loader())
        except Exception as exc:
            with self._lock:
                entry.error = str(exc)[:200] or "连不上云端"
                entry.checked_at = self.clock()
                entry.next_retry = self.monotonic() + self.retry_s
                entry.ready.set()
        else:
            with self._lock:
                entry.data, entry.updated_at = value, self.clock()
                entry.checked_at = entry.updated_at
                entry.error, entry.next_retry = "", 0.0
                entry.ready.set()
            self._persist()

    def _work(self):
        while not self._stop.is_set():
            try:
                job = self._pending.get(timeout=.1)
            except queue.Empty:
                continue
            entry, loader = job
            if self._closed:
                entry.ready.set()
            else:
                self._refresh(entry, loader)

    def _submit(self, entry, loader):
        if not self._workers:
            self._workers = [threading.Thread(target=self._work, name=f"cloud-metadata-{index}", daemon=True)
                             for index in range(3)]
            for worker in self._workers:
                worker.start()
        self._pending.put_nowait((entry, loader))

    def read(self, endpoint: str, kind: str, parameters: dict, loader, *, force: bool = False):
        key = self._key(endpoint, kind, parameters)
        with self._lock:
            if self._closed:
                raise CloudError("云端列表缓存已关闭")
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) >= self.max_entries:
                    disposable = next((old for old, value in self._entries.items() if value.ready.is_set()), None)
                    if disposable is None:
                        raise CloudError("正在读取其他云端列表，请稍后重试")
                    self._entries.pop(disposable)
                entry = self._entries[key] = _Entry(kind)
            self._entries.move_to_end(key)
            had_value = entry.data is not None
            stale = not had_value or self.clock() - entry.updated_at >= self.ttl_s
            if entry.ready.is_set() and (force or (stale and self.monotonic() >= entry.next_retry)):
                entry.ready.clear()
                self._submit(entry, loader)
        if not had_value and not entry.ready.wait(self.wait_s):
            raise CloudError("正在读取云端列表，请稍后刷新")
        with self._lock:
            if entry.data is None:
                raise CloudError(entry.error or "云端还没有可用的列表")
            metadata = {"cached": had_value, "updated_at": entry.updated_at,
                        "checked_at": entry.checked_at, "stale": bool(entry.error) or self.clock() - entry.updated_at >= self.ttl_s,
                        "refreshing": not entry.ready.is_set(), "offline": bool(entry.error), "error": entry.error}
            return copy.deepcopy(entry.data), metadata

    def close(self, *, wait: bool = True):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            while True:
                try:
                    entry, _loader = self._pending.get_nowait()
                    entry.ready.set()
                except queue.Empty:
                    break
        if wait:
            deadline = time.monotonic() + self.wait_s
            for worker in self._workers:
                worker.join(max(0.0, deadline - time.monotonic()))
