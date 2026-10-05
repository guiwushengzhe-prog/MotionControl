"""让程序更新看得见：更了没有、成没成、这一版多了什么。

app_update 一直是安静的：后台下载，下次启动换包，起不来自己退回。安静是对的——
更新失败绝不该拦住玩游戏——但安静过了头，用的人分不清「已经是新的」「下好了等
重启」「上次更新没起来又退回去了」，只能去翻启动窗口里一闪而过的那行字。

这里把每次启动的结果记进用户目录，交给界面说清楚。打不打扰人由更新日志决定
（motioncontrol_shared.changelog）：这一版或跳过的几版里写了新增、变更、移除，
就在打开界面时弹一次「这次更新了什么」；只修问题、纯系统维护的不弹，版本和时间
照样能在「？」菜单里看到。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from motioncontrol_shared.changelog import parse, release_kind, releases_between, version_key

# 这几个名字和 app_update 里的同名常量是同一回事，但**不能从那里 import**：启动器
# 在换包之前就导入了 motioncontrol.app_update，换完包 server.py 再 import 拿到的是
# 内存里那份旧模块。新加的符号旧模块里没有，一导入就 ImportError——更新后的服务
# 起不来，被当成坏包退回。tests/test_update_status.py 会拦住这种 import。
STAGING_NAME = "app_next"
COMPLETE_MARKER = ".complete"
BOOT_MARKER = "app.booting"
RESULT_NAME = "app_update_result.json"
_VERSION_RE = re.compile(r"""^VERSION\s*=\s*["']([^"']+)["']""", re.M)

MAX_EVENTS = 10
CHANGELOG_NAME = "CHANGELOG.md"
# promote() 写下的这几种结果都表示「这次没换成」，界面要照实说。
FAILED_LAUNCHES = {"rolled-back", "rolled-back-nothing", "rollback-failed", "swap-failed", "no-boot-marker"}


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_version(app_dir: Path) -> str:
    """一份 app/ 自己是什么版本，读不出来是空串。只读文本、不导入：导入就是执行它。"""
    match = _VERSION_RE.search(_read_text(Path(app_dir) / "motioncontrol" / "version.py"))
    return match.group(1) if match else ""


def read_changelog(app_dir: Path) -> list[dict]:
    """随包带的更新日志；没有或写坏了就当没有，不影响别的。"""
    try:
        return parse((Path(app_dir) / CHANGELOG_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


# urllib 的原话（"<urlopen error ...>"、"timed out"）给用户看没有意义；原文留在 detail 里。
_NETWORK_MARKS = ("urlopen error", "timed out", "Connection", "Errno", "HTTP Error", "getaddrinfo", "SSL")


def _friendly_error(detail: str) -> str:
    return "连不上更新服务器" if any(mark in detail for mark in _NETWORK_MARKS) else detail


def _brief(release: dict) -> dict:
    return {"version": release["version"], "date": release.get("date", ""),
            "kind": release_kind(release), "sections": release.get("sections", [])}


class UpdateStatus:
    """一份安装的更新记录。线程安全：后台查更新和界面轮询会同时碰它。"""

    def __init__(self, app_dir: Path, history_path: Path, version: str, *, clock=time.time):
        self.app_dir = Path(app_dir).resolve()
        self.root = self.app_dir.parent
        self.history_path = Path(history_path)
        self.version = version
        self._clock = clock
        self._lock = threading.Lock()
        self._history: dict = {}
        self._notes: list[dict] = []
        self._check: dict = {"state": "unknown"}
        self._checking = False

    @property
    def updatable(self) -> bool:
        """只有从发布包的启动器起来才会换包；直接从源码跑 server.py 不会。"""
        return (self.root / "runtime" / "launcher.py").is_file()

    # -- 启动时 -----------------------------------------------------------------

    def observe_launch(self) -> None:
        """服务起来后、boot_ok 删掉 app_previous 之前调一次。"""
        with self._lock:
            history = _read_json(self.history_path)
            now = self._clock()
            events = [item for item in history.get("events", []) if isinstance(item, dict)]
            running = {"version": self.version, "digest": _read_text(self.app_dir / COMPLETE_MARKER)}
            previous = history.get("running") if isinstance(history.get("running"), dict) else None

            launch = _read_json(self.root / RESULT_NAME)
            launch_at = float(launch.get("at") or 0)
            fresh_launch = launch_at > float(history.get("launch_at") or 0)
            if fresh_launch:
                history["launch_at"] = launch_at
            rolled_back = fresh_launch and launch.get("result") in FAILED_LAUNCHES
            if rolled_back:
                events.append({"type": "rolled_back", "at": launch_at, "result": launch.get("result"),
                               "from": launch.get("from", ""), "to": launch.get("to", "") or self.version})

            if previous is None:
                # 头一回有这份记录：新装，或者这个功能出现之前的老安装。刚换过包的话，
                # 旧版本还躺在 app_previous 里（boot_ok 之后才删），从那里读出来，这样
                # 带着这个功能的第一次更新也能告诉人「从几更新到了几」。
                just_promoted = (self.root / BOOT_MARKER).is_file() and (self.root / "app_previous").is_dir()
                before = read_version(self.root / "app_previous") if just_promoted else ""
                if before and before != self.version:
                    events.append({"type": "updated", "from": before, "to": self.version, "at": now})
                history["seen_version"] = before or self.version
                history["running"] = {**running, "since": now if before else 0}
            elif (previous.get("version"), previous.get("digest") or "") != (running["version"], running["digest"]):
                if not rolled_back:
                    events.append({"type": "updated", "from": previous.get("version", ""),
                                   "to": self.version, "at": now})
                history["running"] = {**running, "since": now}

            history["events"] = events[-MAX_EVENTS:]
            seen = str(history.get("seen_version") or self.version)
            self._notes = self._pending_notes_locked(seen)
            if not self._notes and version_key(seen) < version_key(self.version):
                # 跳过的几版只有系统维护：不弹，直接记成看过。
                history["seen_version"] = self.version
            self._history = history
            self._save_locked()

    def _pending_notes_locked(self, seen: str) -> list[dict]:
        releases = releases_between(read_changelog(self.app_dir), seen, self.version)
        if not any(release_kind(release) == "feature" for release in releases):
            return []
        return [_brief(release) for release in releases]

    def _save_locked(self) -> None:
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.history_path.parent,
                                             prefix=".update-history-", delete=False) as stream:
                json.dump(self._history, stream, ensure_ascii=False, indent=2)
                temporary = Path(stream.name)
            os.replace(temporary, self.history_path)
        except OSError:
            pass  # 记不下只是界面少一行字，不能影响启动

    # -- 查更新 -------------------------------------------------------------------

    def begin_check(self) -> bool:
        """抢到这一轮检查就返回 True；已经在查就返回 False。"""
        with self._lock:
            if self._checking:
                return False
            self._checking = True
            self._check = {**self._check, "state": "checking"}
            return True

    def finish_check(self, result: dict) -> None:
        with self._lock:
            self._checking = False
            self._check = {"state": str(result.get("state") or "failed"), "at": self._clock()}
            if result.get("error"):
                detail = str(result["error"])[:200]
                self._check.update(error=_friendly_error(detail), detail=detail)

    def staged(self) -> dict | None:
        """已经下好、等下次启动换上的那一份；同时说清它算不算功能更新。"""
        staging = self.root / STAGING_NAME
        if not _read_text(staging / COMPLETE_MARKER):
            return None
        version = read_version(staging) or self.version
        releases = releases_between(read_changelog(staging), self.version, version)
        kind = "feature" if any(release_kind(release) == "feature" for release in releases) else "system"
        return {"version": version, "kind": kind}

    # -- 界面 ---------------------------------------------------------------------

    def acknowledge(self, version: str) -> None:
        """用户看过「这次更新了什么」。只往前走，不会因为旧页面回传旧版本号而倒退。"""
        with self._lock:
            seen = str(self._history.get("seen_version") or "")
            if version_key(version) > version_key(seen):
                self._history["seen_version"] = str(version)
            if version_key(self._history.get("seen_version")) >= version_key(self.version):
                self._notes = []
            self._save_locked()

    def payload(self) -> dict:
        staged = self.staged() if self.updatable else None
        with self._lock:
            running = self._history.get("running") if isinstance(self._history.get("running"), dict) else {}
            return {
                "version": self.version,
                "updatable": self.updatable,
                "updated_at": float(running.get("since") or 0) or None,
                **self._check,
                "staged": staged,
                "events": list(self._history.get("events", []))[-5:],
                "notes": {"version": self.version, "releases": self._notes} if self._notes else None,
            }
