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
import shutil
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
    request = urllib.request.Request(url, headers={"User-Agent": "MotionControl-PC"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _listing_digest(files: list[dict]) -> str:
    summary = hashlib.sha256()
    for item in files:
        summary.update(_SEPARATOR.join(
            (str(item["path"]), str(item["size"]), str(item["sha256"]), "")
        ).encode("utf-8"))
    return summary.hexdigest()


def check_and_stage(app_dir: Path, base_url: str = DEFAULT_BASE,
                    timeout: float = 20.0) -> dict:
    """看服务器上有没有新的，有就下到暂存目录。不安装。

    任何一步失败都只是"这次没更新"，不抛给调用方：更新失败绝不该拦住玩游戏。
    """
    app_dir = Path(app_dir).resolve()
    staging = app_dir.parent / STAGING_NAME
    result: dict = {"state": "none", "digest": ""}
    try:
        manifest = json.loads(_fetch(base_url.rstrip("/") + MANIFEST_PATH, timeout))
    except Exception as exc:
        return {"state": "failed", "error": str(exc)[:200]}
    if not manifest.get("available"):
        return result

    digest = str(manifest.get("digest") or "")
    result["digest"] = digest
    if digest and digest == _read_marker(app_dir, COMPLETE_MARKER):
        return {**result, "state": "current"}

    total = int(manifest.get("total_bytes") or 0)
    if total > MAX_TOTAL_BYTES:
        return {**result, "state": "refused", "error": "更新包大得不合理"}

    # 验签排在下载之前：一个字节都不该为没签名的包花出去。
    issued_at = verify(manifest, _issued_at(app_dir))
    if issued_at < 0:
        return {**result, "state": "unsigned"}

    if digest and digest == _read_marker(staging, COMPLETE_MARKER):
        return {**result, "state": "ready"}

    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        for item in manifest.get("files", []):
            relative = str(item["path"])
            # 清单是别人给的数据。路径只接受相对、不含 ..，落地前再确认一次它
            # 真的在暂存目录里面。
            if relative.startswith("/") or ".." in relative.split("/"):
                raise ValueError(f"路径不合法：{relative}")
            target = (staging / relative).resolve()
            if staging not in target.parents:
                raise ValueError(f"路径逃出了暂存目录：{relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            url = (base_url.rstrip("/") + FILE_PATH + "?path="
                   + urllib.parse.quote(relative))
            blob = _fetch(url, timeout)
            if hashlib.sha256(blob).hexdigest() != str(item["sha256"]):
                raise ValueError(f"下来的内容对不上：{relative}")
            target.write_bytes(blob)
        (staging / ISSUED_MARKER).write_text(str(issued_at), encoding="utf-8")
        # 记号最后写：它的存在就是"这一份下全了并且验过"。
        (staging / COMPLETE_MARKER).write_text(digest, encoding="utf-8")
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return {**result, "state": "failed", "error": str(exc)[:200]}
    return {**result, "state": "ready"}


def promote(app_dir: Path) -> str:
    """启动时调用：把下好的换进去，并处理上一次没起来的情况。

    必须在导入 motioncontrol.* 之前跑完，否则换掉的是已经加载过的模块。
    """
    app_dir = Path(app_dir).resolve()
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
