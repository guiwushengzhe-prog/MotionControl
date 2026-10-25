"""通用设置的有效副本、待保存状态和限次重试；由内核配置锁保护。"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from .config_transaction import atomic_bytes, recover, replace_documents


class SettingsPersistence:
    RETRY_DELAYS = (2., 5., 15., 30.)

    def __init__(self, path, *, clock=time.monotonic):
        self.path = Path(path)
        self.backup_path = self.path.with_name(self.path.name + ".last-good")
        self.journal_path = self.path.parent / ".general-settings.journal"
        self.clock = clock
        self.instance = uuid.uuid4().hex
        self.generation = time.monotonic_ns() // 1000
        self.version = 0
        self.revision = self.saved_revision = 0
        self.pending = None
        self.attempts = 0
        self.retry_at = 0.
        self.error = self.message = ""
        self.blocked = False
        self._receipt = threading.local()
        recover(self.journal_path)
        self.document = self._load()

    @staticmethod
    def decode(raw):
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("通用设置必须是完整文档")
        return data

    def _archive(self, raw, source=None):
        source = self.path if source is None else Path(source)
        target = source.with_name(source.name + ".invalid-" + hashlib.sha256(raw).hexdigest()[:16])
        if not target.exists():
            atomic_bytes(target, raw)

    def _load(self):
        raw = None
        try:
            raw = self.path.read_bytes()
            data = self.decode(raw)
        except (OSError, ValueError):
            try:
                backup = self.backup_path.read_bytes()
                data = self.decode(backup)
            except (OSError, ValueError):
                if not self.path.exists() and not self.backup_path.exists():
                    return {}
                self.blocked = True
                self.error = "通用设置无法读取，原文件已保留；当前设置尚未保存"
                return {}
            if raw is None and self.path.exists():
                self.blocked = True
                self.error = "通用设置暂时不可读，原文件已保留，已临时使用最近有效设置"
                return data
            try:
                if raw is not None:
                    self._archive(raw)
                atomic_bytes(self.path, backup)
                self.message = "已从最近有效副本恢复通用设置"
            except OSError as error:
                self.pending = copy.deepcopy(data)
                self._failed(error)
            return data
        try:
            if not self.backup_path.is_file() or self.backup_path.read_bytes() != raw:
                atomic_bytes(self.backup_path, raw)
        except OSError as error:
            self.pending = copy.deepcopy(data)
            self._failed(error)
        return data

    def begin_request(self):
        self._receipt.status = None

    def receipt(self):
        return getattr(self._receipt, "status", None)

    def status(self):
        return {"state": "blocked" if self.blocked else "ready" if self.pending is None else
                          "pending" if self.attempts < len(self.RETRY_DELAYS) else "error",
                "error": self.error, "message": self.message, "instance": self.instance,
                "generation": self.generation, "version": self.version,
                "revision": self.revision, "saved_revision": self.saved_revision,
                "attempts": self.attempts, "retry_limit": len(self.RETRY_DELAYS),
                "automatic_retry": self.pending is not None and not self.blocked and self.attempts < len(self.RETRY_DELAYS),
                "retry_after_s": round(max(0., self.retry_at - self.clock()), 1)}

    def _failed(self, error):
        self.version += 1
        code = getattr(error, "winerror", None)
        if getattr(error, "errno", None) == errno.ENOSPC or code == 112:
            self.error = "磁盘空间不足，设置已在本次生效，尚未保存"
        elif getattr(error, "errno", None) in {errno.EACCES, errno.EPERM} or code in {5, 32, 33}:
            self.error = "配置文件被占用或目录不可写，设置已在本次生效，尚未保存"
        else:
            self.error = "暂时无法写入配置文件，设置已在本次生效，尚未保存"
        self.attempts += 1
        self.retry_at = self.clock() + self.RETRY_DELAYS[min(self.attempts - 1, len(self.RETRY_DELAYS) - 1)]

    def documents(self, payload):
        if self.blocked:
            raise ValueError(self.error + "；请先恢复备份，或确认保存当前设置")
        return {self.path: payload, self.backup_path: payload}

    def committed(self, payload, *, revision=None):
        self.version += 1
        self.revision = self.revision + 1 if revision is None else revision
        self.saved_revision = self.revision
        self.document = copy.deepcopy(payload)
        self.pending = None
        self.attempts = 0
        self.retry_at = 0.
        self.error = ""
        self._receipt.status = self.status()

    def save(self, payload):
        self.version += 1
        self.revision += 1
        self.pending = copy.deepcopy(payload)
        if not self.blocked and self.attempts < len(self.RETRY_DELAYS) and self.clock() >= self.retry_at:
            self._attempt()
        self._receipt.status = self.status()
        return self._receipt.status

    def _attempt(self):
        try:
            payload = self.pending
            replace_documents(self.documents(payload), self.journal_path)
            self.committed(payload, revision=self.revision)
        except (OSError, ValueError) as error:
            self._failed(error)

    def retry(self, *, force=False, repair_payload=None):
        if self.blocked:
            if repair_payload is None:
                return self.status()
            try:
                for source in (self.path, self.backup_path):
                    if source.exists():
                        self._archive(source.read_bytes(), source)
                self.pending = copy.deepcopy(repair_payload)
                self.revision += 1
                self.blocked = False
            except OSError as error:
                self._failed(error)
                self._receipt.status = self.status()
                return self._receipt.status
        if force:
            self.version += 1
            self.attempts = 0
            self.retry_at = 0.
        if self.pending is not None and self.attempts < len(self.RETRY_DELAYS) and self.clock() >= self.retry_at:
            self._attempt()
        self._receipt.status = self.status()
        return self._receipt.status

    def snapshot(self):
        return {name: copy.deepcopy(getattr(self, name)) for name in
                ("document", "revision", "saved_revision", "pending", "attempts", "retry_at", "error", "message", "blocked")}

    def restore(self, snapshot):
        for name, value in snapshot.items():
            setattr(self, name, value)
        self.version += 1
        self._receipt.status = None
