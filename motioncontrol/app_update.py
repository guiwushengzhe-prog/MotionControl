"""从服务器更新电脑端自己的程序本体。

为什么必须是第一版就有：发布包 174 MB，其中 324 MB 是 Python 运行时、84 MB 是
模型，这两样几乎永不变。真正会改的 app/ 只有 2 MB 出头。没有这条通道，每修一个
小问题都要让所有人重下 174 MB——而这条通道补不进已经发出去的版本里，装了旧版的
人永远只能重下。手机端网页包是同一个道理，那边先做了。

只信一把公钥
  包落地就是要执行的代码。谁能把清单递到这台机器面前，谁就决定它执行什么——
  "它是从我服务器来的"是假设，不是论证。所以校验签名，验不过就不装，继续跑现
  在这一份。

  这条也补不了：不验签的第一版会让所有已装机器永远接受不验签的包，而修它的补丁
  要走的正是这条不设防的通道。

换包只在启动那一刻发生
  在跑着的时候改它脚下的 .py，会得到一个半新半旧的程序。所以下载进 app_next，
  启动时才换——那一刻还没有模块被导入。

跑不起来的包自己退回去
  签名只证明包是我发的，不证明它跑得起来：一个能让服务起不来的改动照样签得好好
  的。启动时留个记号，服务真的起来了就清掉；记号要是活到下次启动，说明上一次没
  起来，直接把包丢掉退回原来那份。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 和手机端网页包同一把。同一个发布者两把钥匙，等于两个可能弄丢的东西。
PUBLIC_KEY_B64 = (
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE6R7hkxscJdT82U2Nr6A82kXrkC7Hci25"
    "CGeaCcodPvZLFQFUoBcT+TF+KM+wzYHSSVz21nTu2yymodO98rKu5g=="
)

DEFAULT_BASE = "https://motioncontrol.guiwu-aware.icu"
MANIFEST_PATH = "/api/v1/app-update/pc"
FILE_PATH = "/api/v1/app-update/pc/file"

STAGING_NAME = "app_next"
COMPLETE_MARKER = ".complete"
ISSUED_MARKER = ".issued"
BOOT_MARKER = "app.booting"
_SEPARATOR = chr(0)
_CHUNK = 1 << 16
# 程序本体只有 2 MB 出头。比这大得多的东西不该从这条通道来，与其下完再发现不对，
# 不如一开始就不下。
MAX_TOTAL_BYTES = 64 << 20
MAX_MANIFEST_BYTES = 1 << 20

# 属于这一份安装、不属于代码的文件。它们指向发布包自己的相对位置（../models、
# ../native/ViGEmClient.dll），换包时必须从旧的那份搬过来——跟着更新包走的话，
# 每次更新都会把这台机器的模型位置覆盖掉，然后语音和手柄一起失灵，而界面上只
# 会说"模型找不到"，没人会想到是更新干的。
KEEP_FROM_OLD = (
    "config/model_root.txt",
    "config/vosk_model_path.txt",
    "config/vigemclient_dll.txt",
    "config/sherpa_kws_model_path.txt",
    "config/funasr_python_path.txt",
    "config/funasr_seaco_model_path.txt",
    "config/funasr_vad_model_path.txt",
)


def _load_public_key():
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    return load_der_public_key(base64.b64decode(PUBLIC_KEY_B64))


def verify(manifest: dict, previous_issued_at: float) -> float:
    """签名对不对，以及是不是比手上这份新。对了返回签发时间，否则返回 -1。

    时间戳是用来挡降级的：我自己的旧包也是签对的，没有这一项，中间人可以把上个
    月那版递过来并且被相信。
    """
    payload_b64 = str(manifest.get("payload") or "")
    signature_b64 = str(manifest.get("signature") or "")
    if not payload_b64 or not signature_b64:
        return -1.0
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        payload = base64.b64decode(payload_b64)
        _load_public_key().verify(
            base64.b64decode(signature_b64), payload, ec.ECDSA(hashes.SHA256()))
        signed = json.loads(payload)
    except Exception:
        return -1.0
    if str(signed.get("digest") or "") != str(manifest.get("digest") or ""):
        return -1.0
    issued_at = float(signed.get("issued_at") or 0)
    if issued_at <= 0 or issued_at < previous_issued_at:
        return -1.0
    return issued_at


def _read_marker(directory: Path, name: str) -> str:
    try:
        return (directory / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _issued_at(directory: Path) -> float:
    try:
        return float(_read_marker(directory, ISSUED_MARKER) or 0)
    except ValueError:
        return 0.0


def _fetch(url: str, timeout: float) -> bytes:
    """Only the small manifest is held in memory; program files stream to disk."""
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(url, headers={"User-Agent": "MotionControl-PC"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        chunks = []
        total = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("更新清单读取超时")
            chunk = getattr(response, "read1", response.read)(_CHUNK)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > MAX_MANIFEST_BYTES:
                raise ValueError("更新清单过大")
            chunks.append(chunk)


class _Cancelled(Exception):
    pass


class _DownloadBudget:
    def __init__(self, timeout: float, total_timeout_s: float,
                 cancel_event: threading.Event | None):
        self.timeout = max(0.01, float(timeout))
        self.deadline = time.monotonic() + max(0.0, float(total_timeout_s))
        self.cancel_event = cancel_event

    def remaining(self) -> float:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise _Cancelled("更新下载已取消")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("更新下载超过本次时间预算，下次继续")
        return remaining

    def io_timeout(self) -> float:
        # An event cannot interrupt urllib's blocking read. Keep any single
        # socket wait short when cancellation is requested by the caller.
        return min(self.timeout, self.remaining(), 2.0 if self.cancel_event is not None else self.timeout)

    def pause(self, seconds: float) -> None:
        seconds = min(seconds, self.remaining())
        if self.cancel_event is not None:
            self.cancel_event.wait(seconds)
        else:
            time.sleep(seconds)
        self.remaining()


def _retry(operation, budget: _DownloadBudget, retries: int):
    for attempt in range(max(0, min(int(retries), 3)) + 1):
        budget.remaining()
        try:
            return operation()
        except urllib.error.HTTPError as exc:
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                raise
            error = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            error = exc
        if attempt >= max(0, min(int(retries), 3)):
            raise error
        budget.pause(0.25 * (2 ** attempt))


def _digest_file(path: Path, budget: _DownloadBudget) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            budget.remaining()
            chunk = stream.read(_CHUNK)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _download_file(url: str, target: Path, size: int, expected: str,
                   budget: _DownloadBudget) -> None:
    """Publish one complete, verified file; a partial transfer never replaces it."""
    temporary = None
    request = urllib.request.Request(url, headers={"User-Agent": "MotionControl-PC"})
    try:
        with urllib.request.urlopen(request, timeout=budget.io_timeout()) as response:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".download-", delete=False) as output:
                temporary = Path(output.name)
                digest = hashlib.sha256()
                received = 0
                while True:
                    budget.remaining()
                    # read1 returns available data instead of waiting to fill
                    # a large buffer, so the overall deadline is checked even
                    # when a slow peer keeps sending a few bytes at a time.
                    reader = getattr(response, "read1", response.read)
                    chunk = reader(min(_CHUNK, size - received + 1))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > size:
                        raise ValueError(f"更新文件超过清单大小：{target.name}")
                    output.write(chunk)
                    digest.update(chunk)
        if received != size or digest.hexdigest() != expected:
            raise ValueError(f"下来的内容对不上：{target.name}")
        budget.remaining()
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _remove_empty_directories(root: Path) -> None:
    if not root.is_dir():
        return
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def _listing_digest(files: list[dict]) -> str:
    summary = hashlib.sha256()
    for item in files:
        summary.update(_SEPARATOR.join(
            (str(item["path"]), str(item["size"]), str(item["sha256"]), "")
        ).encode("utf-8"))
    return summary.hexdigest()


def check_and_stage(app_dir: Path, base_url: str = DEFAULT_BASE,
                    timeout: float = 20.0, *, total_timeout_s: float = 120.0,
                    cancel_event: threading.Event | None = None,
                    retries: int = 2) -> dict:
    """看服务器上有没有新的，有就下到暂存目录。不安装。

    任何一步失败都只是"这次没更新"，不抛给调用方：更新失败绝不该拦住玩游戏。
    """
    app_dir = Path(app_dir).resolve()
    staging = app_dir.parent / STAGING_NAME
    budget = _DownloadBudget(timeout, total_timeout_s, cancel_event)
    result: dict = {"state": "none", "digest": ""}
    try:
        manifest = json.loads(_retry(
            lambda: _fetch(base_url.rstrip("/") + MANIFEST_PATH, budget.io_timeout()), budget, retries))
        if not isinstance(manifest, dict):
            raise ValueError("更新清单格式无效")
    except _Cancelled as exc:
        return {"state": "cancelled", "error": str(exc)}
    except Exception as exc:
        return {"state": "failed", "error": str(exc)[:200]}
    if not manifest.get("available"):
        return result

    digest = str(manifest.get("digest") or "")
    result["digest"] = digest
    if digest and digest == _read_marker(app_dir, COMPLETE_MARKER):
        return {**result, "state": "current"}

    try:
        total = int(manifest.get("total_bytes") or 0)
    except (TypeError, ValueError):
        return {**result, "state": "failed", "error": "更新清单大小无效"}
    if total > MAX_TOTAL_BYTES:
        return {**result, "state": "refused", "error": "更新包大得不合理"}

    # 验签排在下载之前：一个字节都不该为没签名的包花出去。
    issued_at = verify(manifest, _issued_at(app_dir))
    if issued_at < 0:
        return {**result, "state": "unsigned"}

    if digest and digest == _read_marker(staging, COMPLETE_MARKER):
        return {**result, "state": "ready"}

    try:
        files = manifest.get("files", [])
        if not isinstance(files, list) or not files or _listing_digest(files) != digest:
            raise ValueError("更新清单和签名内容对不上")
        targets = []
        wanted = set()
        sizes = 0
        # Validate the entire listing before altering an earlier staging area.
        for item in files:
            relative = str(item["path"])
            size = int(item["size"])
            if size < 0:
                raise ValueError("更新文件大小无效")
            sizes += size
            if sizes > MAX_TOTAL_BYTES:
                raise ValueError("更新包大得不合理")
            if relative.startswith("/") or ".." in relative.replace("\\", "/").split("/"):
                raise ValueError(f"路径不合法：{relative}")
            target = (staging / relative).resolve()
            if staging not in target.parents or target in wanted:
                raise ValueError(f"更新路径无效：{relative}")
            wanted.add(target)
            targets.append((relative, target, size, str(item["sha256"])))
        if sizes != total:
            raise ValueError("更新清单总大小不一致")
        staging.mkdir(parents=True, exist_ok=True)
        (staging / COMPLETE_MARKER).unlink(missing_ok=True)
        for relative, target, size, expected in targets:
            budget.remaining()
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.stat().st_size == size and _digest_file(target, budget) == expected:
                continue
            live = (app_dir / relative).resolve()
            if app_dir in live.parents and live.is_file() and live.stat().st_size == size and _digest_file(live, budget) == expected:
                shutil.copy2(live, target)
                # The running installation can still change a generated file
                # between hashing and copying it. Only reuse verified bytes.
                if target.stat().st_size == size and _digest_file(target, budget) == expected:
                    continue
            url = (base_url.rstrip("/") + FILE_PATH + "?path="
                   + urllib.parse.quote(relative))
            _retry(lambda: _download_file(url, target, size, expected, budget), budget, retries)
        # Remove retired files only after all desired ones have arrived.
        for existing in staging.rglob("*"):
            if existing.is_file() and existing.resolve() not in wanted:
                existing.unlink()
        budget.remaining()
        (staging / ISSUED_MARKER).write_text(str(issued_at), encoding="utf-8")
        # 记号最后写：它的存在就是"这一份下全了并且验过"。
        (staging / COMPLETE_MARKER).write_text(digest, encoding="utf-8")
    except Exception as exc:
        _remove_empty_directories(staging)
        return {**result, "state": "cancelled" if isinstance(exc, _Cancelled) else "failed",
                "error": str(exc)[:200]}
    return {**result, "state": "ready"}


# 每次启动换包或退回的结果，写在包根目录——app/ 之外，因为退回会删掉 app/。以前
# 它只打印在启动窗口里，用的人分不清"更新成了""没东西可更""上次更新没起来又退
# 回去了"这三种情况。服务起来以后由 update_status 读出来交给界面。
RESULT_NAME = "app_update_result.json"
_VERSION_RE = re.compile(r"""^VERSION\s*=\s*["']([^"']+)["']""", re.M)


def read_version(app_dir: Path) -> str:
    """一份 app/ 自己是什么版本，读不出来是空串。只读文本、不导入：导入就是执行它。"""
    try:
        text = (Path(app_dir) / "motioncontrol" / "version.py").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _VERSION_RE.search(text)
    return match.group(1) if match else ""


def promote(app_dir: Path) -> str:
    """启动时调用：把下好的换进去，并处理上一次没起来的情况。

    必须在导入 motioncontrol.* 之前跑完，否则换掉的是已经加载过的模块。
    """
    app_dir = Path(app_dir).resolve()
    before = read_version(app_dir)
    result = _promote(app_dir)
    if result != "nothing-staged":
        record = {"result": result, "at": time.time(), "from": before, "to": read_version(app_dir)}
        try:
            (app_dir.parent / RESULT_NAME).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # 记不下结果不影响换包本身
    return result


def _promote(app_dir: Path) -> str:
    root = app_dir.parent
    staging = root / STAGING_NAME
    boot_mark = root / BOOT_MARKER

    # 上一次带着新包启动，服务没能跑到"我起来了"那一步。
    if boot_mark.is_file():
        try:
            boot_mark.unlink()
            backup = root / "app_previous"
            if backup.is_dir():
                shutil.rmtree(app_dir, ignore_errors=True)
                os.replace(backup, app_dir)
                return "rolled-back"
        except OSError:
            return "rollback-failed"
        return "rolled-back-nothing"

    if not _read_marker(staging, COMPLETE_MARKER):
        return "nothing-staged"
    try:
        backup = root / "app_previous"
        shutil.rmtree(backup, ignore_errors=True)
        os.replace(app_dir, backup)
        # 先搬这一份安装自己的路径文件，再把新的放上去：顺序反过来的话，新包里
        # 万一带了同名文件就会赢，而它写的是打包那台机器的路径。
        for relative in KEEP_FROM_OLD:
            source = backup / relative
            if not source.is_file():
                continue
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        os.replace(staging, app_dir)
    except OSError:
        return "swap-failed"
    try:
        boot_mark.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        # 记不上号就不敢用它：宁可少一次更新，也不能失去退回的能力。
        try:
            shutil.rmtree(app_dir, ignore_errors=True)
            os.replace(root / "app_previous", app_dir)
        except OSError:
            pass
        return "no-boot-marker"
    return "promoted"


def boot_ok(app_dir: Path) -> None:
    """服务真的起来了。清掉记号，这一份就算站住了。"""
    root = Path(app_dir).resolve().parent
    try:
        (root / BOOT_MARKER).unlink(missing_ok=True)
        shutil.rmtree(root / "app_previous", ignore_errors=True)
    except OSError:
        pass
