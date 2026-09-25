"""这台电脑从云端官方动作库下载了哪些动作：``pose_actions.json`` 的读写和验签。

程序只自带原地踏步和小腿向后抬起。别的动作（下蹲、开合跳……）要从官方动作库下载，
识别规则就在动作文件里——所以这里存的东西决定这台电脑能认出什么动作、认成什么样。

## 只信一把公钥

动作文件装上就决定把什么动作认成哪个键。谁能把文件递到这台电脑面前，谁就能改它认
什么；"它是从我服务器来的"是假设，不是论证。所以和软件更新同一把公钥（app_update
里那一把）：下载时验，每次启动读盘时再验一遍——本机的文件也可能被别的程序改过。
验不过的直接丢掉，不装、不加载。

存的是云端发来的**原始字节**和签名，不是解析后的对象：签名签的是那串字节，重新
序列化一遍就对不上了。

## 没下载的动作

别人分享的配置可能用到了你没下载的动作。那一行照样留在配置里，只是认不出来；
界面上说清楚"要先去官方动作库下载"。以后自动下载、和收费挂钩都在这之上加。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path

from motioncontrol_shared.pose_library import canonical_bytes, normalize_action

SCHEMA = "motioncontrol.installed_pose_actions.v1"
# 一个动作文件实测 3 KB 上下。远大于这个的不会是动作文件。
MAX_DOCUMENT_BYTES = 64 * 1024


class PoseDownloadError(Exception):
    pass


def _public_key():
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    from motioncontrol.app_update import PUBLIC_KEY_B64

    return load_der_public_key(base64.b64decode(PUBLIC_KEY_B64))


def verify(document_b64: str, signature_b64: str, public_key=None) -> dict:
    """验签、解析、校验，返回动作文件。任何一步不过都抛 PoseDownloadError。"""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:  # pragma: no cover - 发布包里一定有，开发环境可能没装
        raise PoseDownloadError("缺少 cryptography，没法验证动作文件的签名") from None
    try:
        payload = base64.b64decode(str(document_b64), validate=True)
        signature = base64.b64decode(str(signature_b64), validate=True)
    except (binascii.Error, ValueError):
        raise PoseDownloadError("动作文件格式不对") from None
    if not payload or len(payload) > MAX_DOCUMENT_BYTES:
        raise PoseDownloadError("动作文件大小不对")
    try:
        (public_key or _public_key()).verify(signature, payload, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        raise PoseDownloadError("动作文件的签名验不过，不是官方发布的，已拒绝") from None
    try:
        raw = json.loads(payload.decode("utf-8"))
        doc = normalize_action(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PoseDownloadError("动作文件不是有效的 JSON") from None
    except ValueError as exc:
        # 签名对、本地规则却不认：多半是这个动作要更新的电脑端才认得的写法。
        raise PoseDownloadError(f"这个动作装不上：{exc}") from None
    if canonical_bytes(doc) != payload:
        raise PoseDownloadError("动作文件和本机的规则版本对不上，请更新电脑端后再试")
    return doc


class PoseActionStore:
    """线程安全交给调用方，和 MacroStore 一样：server 那边所有写入都在一把锁里。"""

    def __init__(self, path: Path, public_key=None):
        self.path = Path(path)
        self.public_key = public_key
        # 编号 → {"doc": 动作文件, "document": 原始字节的 base64, "signature": 签名}
        self.items: dict[str, dict] = {}
        self.last_error = ""
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏文件不该让程序起不来
            self.last_error = f"下载的动作读取失败：{exc}"
            return
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            self.last_error = "下载的动作文件版本不认识，已忽略"
            return
        dropped = []
        for item in data.get("actions", []):
            if not isinstance(item, dict):
                continue
            try:
                doc = verify(item.get("document", ""), item.get("signature", ""), self.public_key)
            except PoseDownloadError as exc:
                dropped.append(str(exc))
                continue
            self.items[doc["id"]] = {"doc": doc, "document": item["document"], "signature": item["signature"]}
        if dropped:
            self.last_error = f"有 {len(dropped)} 个下载的动作验不过，没有加载：{dropped[0]}"

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "schema": SCHEMA,
            "actions": [{"document": item["document"], "signature": item["signature"]}
                        for item in self.items.values()],
        }, ensure_ascii=False, indent=2)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, self.path)

    def docs(self) -> list[dict]:
        return [item["doc"] for item in self.items.values()]

    def revision(self, ident: str) -> int:
        item = self.items.get(ident)
        return int(item["doc"]["revision"]) if item else 0

    def install(self, document_b64: str, signature_b64: str, expected_id: str = "") -> dict:
        """装一个（或更新到新版本）。先验签，验不过什么都不改。"""
        doc = verify(document_b64, signature_b64, self.public_key)
        if expected_id and doc["id"] != expected_id:
            raise PoseDownloadError("下载到的动作和要的不是同一个，已拒绝")
        self.items[doc["id"]] = {"doc": doc, "document": str(document_b64), "signature": str(signature_b64)}
        self._save()
        self.last_error = ""
        return doc

    def remove(self, ident: str) -> bool:
        if self.items.pop(str(ident), None) is None:
            return False
        self._save()
        return True
