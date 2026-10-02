"""启动时异步检查；缺规则只自动申请一次，取消后允许在界面重试。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading


class WindowsFirewall:
    def __init__(self, preference: Path):
        self.preference = preference
        self.port = 8765
        self.enabled = True
        self.program = str(Path(sys.executable).resolve())
        self._windows = os.name == "nt"
        self._lock = threading.Lock()
        self._busy = False
        self._status = {"state": "unchecked", "message": "正在检查连接权限"}

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def ensure_async(self, port: int | None = None, *, retry: bool = False) -> None:
        with self._lock:
            if not self.enabled:
                self._status = {"state": "local", "message": ""}
                return
            if self._busy:
                return
            if port is not None:
                self.port = port
            self._busy = True
            self._status = {"state": "checking", "message": "正在检查连接权限"}
        threading.Thread(target=self._ensure, args=(retry,), name="motion-firewall", daemon=True).start()

    def _ensure(self, retry: bool) -> None:
        try:
            if not self._windows:
                result = {"state": "unsupported", "message": ""}
            else:
                result = self._run("Check")
                key = hashlib.sha256(f"{self.program.casefold()}:{self.port}".encode()).hexdigest()[:16]
                try:
                    attempted = json.loads(self.preference.read_text(encoding="utf-8")).get(key, False)
                except (OSError, ValueError, AttributeError):
                    attempted = False
                if result.get("state") == "missing":
                    if retry or not attempted:
                        with self._lock:
                            self._status = {"state": "authorizing", "message": "请在 Windows 提示中允许配置连接权限"}
                        # 在弹窗前记住尝试；拒绝后不能每次打开软件都再弹一次。
                        try:
                            preferences = json.loads(self.preference.read_text(encoding="utf-8"))
                            if not isinstance(preferences, dict):
                                preferences = {}
                        except (OSError, ValueError):
                            preferences = {}
                        preferences[key] = True
                        self.preference.parent.mkdir(parents=True, exist_ok=True)
                        self.preference.write_text(json.dumps(preferences), encoding="utf-8")
                        result = self._run("Ensure")
                    else:
                        result = {"state": "missing", "message": "局域网连接权限未配置，可点此重试"}
        except Exception as exc:
            result = {"state": "error", "message": "连接权限配置未完成，可重试", "detail": str(exc)}
        with self._lock:
            self._status = result
            self._busy = False

    def _run(self, mode: str) -> dict:
        shell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        script = str(Path(__file__).with_suffix(".ps1"))
        command = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script,
                   "-Mode", mode, "-Program", self.program, "-Port", str(self.port)]
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
        if process.returncode:
            raise RuntimeError(process.stderr.strip() or "Windows 防火墙检查失败")
        return json.loads(process.stdout.strip())
