"""运动记录和云端账号之间来回搬：登录了就先把云端的拉下来，再把本机改过的传上去。

一分钟一轮，登录、退出、结束一次锻炼时立刻来一轮。同步不碰体感控制：在自己的线程里，
连不上就等下一轮，什么都不丢——本机记录本来就完整，云端只是备份和换设备用的。
合并规则在 motioncontrol_shared.fitness_schema，两边同一份，传几遍都一样。
"""

from __future__ import annotations

import threading
import time
from urllib.parse import quote

from motioncontrol.cloud_account import CloudAccount, CloudAccountError, SignedOut

INTERVAL_S = 60.0
PAGES_PER_ROUND = 20


class FitnessSync:
    def __init__(self, store, account: CloudAccount, *, interval=INTERVAL_S, background=True):
        self.store, self.account, self.interval = store, account, interval
        self.last_synced_at: float | None = None   # time.time()
        self.error = ""
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._round = threading.Lock()
        self._account_token = account.token
        account.subscribe(self._account_changed)
        self._thread = None
        if background:
            self._thread = threading.Thread(target=self._loop, name="fitness-cloud-sync", daemon=True)
            self._thread.start()

    def status(self) -> dict:
        return {"last_synced_at": self.last_synced_at, "error": self.error}

    def wake(self) -> None:
        self._wake.set()

    def _account_changed(self) -> None:
        # 换了账号（或者重新登录）：这个账号在云端有什么、本机哪些还没传给它，都要从头算。
        token = self.account.token
        if token and token != self._account_token:
            self.store.cloud_reset()
        self._account_token = token
        self.last_synced_at = None
        self.error = ""
        self.wake()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sync_once()
            self._wake.wait(self.interval)
            self._wake.clear()

    def sync_once(self) -> bool:
        """来一轮：拉、合并、传。没登录就什么都不做。返回这一轮成没成。"""
        if not self.account.signed_in:
            return False
        with self._round:
            try:
                self._pull()
                self._push()
            except SignedOut as exc:
                self.error = str(exc)
                return False
            except (CloudAccountError, ValueError) as exc:
                self.error = str(exc)
                return False
            self.error = ""
            self.last_synced_at = time.time()
            return True

    def _pull(self) -> None:
        cursor = self.store.cloud["cursor"]
        for _ in range(PAGES_PER_ROUND):
            page = self.account.request("GET", "/fitness?cursor=" + quote(cursor, safe=""))
            cursor = str(page.get("cursor") or "")
            self.store.merge_remote(page, cursor=cursor)
            if not page.get("more"):
                return

    def _push(self) -> None:
        pending = self.store.cloud_pending()
        profile = pending["profile"]
        if profile is not None:
            stored = self.account.request("PUT", "/fitness/profile", {"profile": profile})["profile"]
            # 云端那份更新（另一台电脑后改的）：以它为准。
            self.store.merge_remote({"profile": stored})
            self.store.cloud_mark_profile(profile["updated_at_ms"])
        sent: set[str] = set()
        while True:
            sessions = pending["sessions"]
            self.account.request("POST", "/fitness/sessions", {"sessions": sessions, "checkins": pending["checkins"]})
            self.store.cloud_mark_uploaded(sessions)
            sent.update(doc["session_id"] for doc in sessions)
            pending = self.store.cloud_pending()
            # 正在记录的那次每一帧都在变，传完又是"没传过的"：这一轮传过的就等下一轮。
            if not {doc["session_id"] for doc in pending["sessions"]} - sent:
                return

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
