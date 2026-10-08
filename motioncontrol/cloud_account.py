"""这台电脑登录的云端账号。

登录不在电脑上输密码：电脑向云端要一个码，打开浏览器，人在网站上登录、点「允许」，
电脑这边每隔几秒问一次，允许了就拿到一个只能同步运动记录的凭证（见
cloud/app/routers/device.py）。凭证存在用户目录的 cloud_account.json，绝不跟着配置同步
（sync_allowlist 里是 device_configuration）。

和 cloud_client 一样只用标准库、超时短、出错就是一句中文：云端连不上，本机的控制和
运动记录照常，只是不同步。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from motioncontrol.cloud_client import CONNECT_TIMEOUT_S, USER_AGENT

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class CloudAccountError(Exception):
    """一句能直接给人看的话。"""


class SignedOut(CloudAccountError):
    """凭证被收回或者过期了：本机也当成退出了。"""


def call(base: str, method: str, path: str, body=None, token: str = "", timeout: float = CONNECT_TIMEOUT_S):
    """调一次云端接口，返回解析好的 JSON（204 返回 None）。"""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{base.rstrip('/')}/api/v1{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if response.status == 204 or not raw:
                return None
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(64 * 1024).decode("utf-8")).get("detail")
        except Exception:  # noqa: BLE001 - 错误页不是 JSON 也照样要报
            detail = None
        if exc.code == 401 and token:
            raise SignedOut(detail if isinstance(detail, str) else "这台电脑的登录已失效，请重新登录") from None
        if exc.code == 404 and path.startswith(("/device", "/fitness")) and not isinstance(detail, str):
            raise CloudAccountError("云端还没有这个功能，等云端更新后再试") from None
        raise CloudAccountError(detail if isinstance(detail, str) else f"云端返回错误（{exc.code}）") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CloudAccountError("连不上云端，检查一下网络") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CloudAccountError("云端返回的内容过大")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CloudAccountError("云端返回的不是有效的 JSON") from None


class CloudAccount:
    def __init__(self, path: Path, endpoint, *, name: str = "MotionControl", caller=call, clock=time.monotonic):
        self.path = Path(path)
        self._endpoint = endpoint          # 现在用哪个云端；用户可以改，所以每次都问
        self.name = name
        self._call = caller
        self._clock = clock
        self._lock = threading.RLock()
        self._saved: dict = {}
        self._login: dict | None = None    # 正在等浏览器里点允许的那一次
        self._listeners: list = []
        self.error = ""
        self._load()

    # ---------- 存盘 ----------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and isinstance(data.get("token"), str) and isinstance(data.get("endpoint"), str):
            self._saved = data

    def _save(self) -> None:
        if not self._saved:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._saved, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.path)

    # ---------- 状态 ----------

    @property
    def base(self) -> str:
        return str(self._endpoint()).rstrip("/")

    @property
    def token(self) -> str:
        """登录的是现在这个云端才算数：改了云端地址，原来的凭证在新地方没用。"""
        with self._lock:
            return self._saved.get("token", "") if self._saved.get("endpoint") == self.base else ""

    @property
    def signed_in(self) -> bool:
        return bool(self.token)

    def subscribe(self, listener) -> None:
        """登录、退出时叫一声（同步要从头拉一遍）。"""
        self._listeners.append(listener)

    def status(self) -> dict:
        with self._lock:
            login = self._login
            if self.signed_in:
                return {"state": "signed_in", "display_name": self._saved.get("display_name", ""), "error": self.error}
            if login is not None:
                return {"state": "waiting", "user_code": login["user_code"],
                        "verification_uri": login["verification_uri_complete"], "error": self.error}
            return {"state": "signed_out", "error": self.error}

    # ---------- 登录、退出 ----------

    def begin_login(self) -> dict:
        """要一个码，开始等浏览器里点允许。返回状态，里面有要打开的网址。"""
        base = self.base
        started = self._call(base, "POST", "/device/authorize", {"name": self.name, "scopes": ["fitness"]})
        with self._lock:
            self.error = ""
            self._login = {**started, "base": base, "deadline": self._clock() + float(started.get("expires_in", 600)),
                           "id": object()}
            login = self._login
        threading.Thread(target=self._wait, args=(login,), name="cloud-login", daemon=True).start()
        return self.status()

    def cancel_login(self) -> None:
        with self._lock:
            self._login = None

    def _wait(self, login: dict) -> None:
        interval = max(1.0, float(login.get("interval", 3)))
        while True:
            time.sleep(interval)
            with self._lock:
                if self._login is not login:
                    return
                if self._clock() > login["deadline"]:
                    self._login, self.error = None, "登录码过期了，请重新点登录"
                    return
            try:
                result = self._call(login["base"], "POST", "/device/token", {"device_code": login["device_code"]})
            except CloudAccountError as exc:
                with self._lock:
                    if self._login is login and "过期" in str(exc):
                        self._login, self.error = None, str(exc)
                        return
                continue  # 网络抖一下不算失败，接着等
            if result.get("status") != "approved":
                continue
            with self._lock:
                if self._login is not login:
                    return
                self._login = None
                self._saved = {"endpoint": login["base"], "token": result["access_token"],
                               "display_name": result.get("display_name", "")}
                self.error = ""
                self._save()
            self._changed()
            return

    def logout(self) -> None:
        token, base = self.token, self.base
        with self._lock:
            self._saved, self._login, self.error = {}, None, ""
            self._save()
        if token:
            try:
                self._call(base, "POST", "/device/logout", token=token)
            except CloudAccountError:
                pass  # 云端连不上也照样退出本机；那个凭证没人知道，放着也无害
        self._changed()

    def request(self, method: str, path: str, body=None):
        """带着凭证调接口。凭证失效就本机退出，再把错报出去。"""
        token = self.token
        if not token:
            raise SignedOut("这台电脑还没登录云端账号")
        try:
            return self._call(self.base, method, path, body, token=token, timeout=15.0)
        except SignedOut as exc:
            with self._lock:
                if self._saved.get("token") == token:
                    self._saved = {}
                    self._save()
                self.error = str(exc)
            self._changed()
            raise

    def _changed(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - 一个监听坏了不影响登录本身
                pass
