"""给官方动作库签名。签了名的动作云端才发布，电脑端也只装验得过签名的。

为什么要签：动作文件里有识别规则，装上就决定这台电脑把什么动作认成什么键。谁能把
文件递到电脑面前、谁就能改它认什么——"它是从我服务器来的"是假设，不是论证。规则是
数据、执行不了代码（见 pose_rules），但一条被改过的规则照样能让人一抬手就按下急停
以外的任何键。所以和软件更新、手机网页包一样：只信一把公钥。

用的就是那一把私钥（config/phone_web_signing_key.txt 里写着路径，不进仓库）。同一个
发布者两把钥匙，等于两个可能弄丢的东西。

签的是动作文件规范化之后的字节（pose_library.canonical_bytes），不是磁盘上那份带
缩进的文件：改缩进、换字段顺序不用重签，改了任何一个值就必须重签。

    python tools/sign_pose_library.py            # 签 cloud/official_poses/ 下全部动作
    python tools/sign_pose_library.py --check    # 只检查，不需要私钥
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from motioncontrol_shared.pose_library import canonical_bytes, normalize_action  # noqa: E402
from sign_phone_web import key_path, load_backend  # noqa: E402

OFFICIAL = ROOT / "cloud" / "official_poses"
SIGNATURES = OFFICIAL / "signatures.json"
SIGNATURES_SCHEMA = "motioncontrol.pose_signatures.v1"


def official_documents(folder: Path = OFFICIAL) -> list[tuple[dict, bytes]]:
    """(校验过的动作文件, 要签的那串字节)。有一个校验不过就整批不签。"""
    out = []
    for path in sorted(folder.glob("*.json")):
        if path.name == SIGNATURES.name:
            continue
        try:
            doc = normalize_action(json.loads(path.read_text(encoding="utf-8")))
        except ValueError as exc:
            raise SystemExit(f"{path.name}：{exc}")
        if doc["id"] != path.stem:
            raise SystemExit(f"{path.name}：文件名要和 id（{doc['id']}）一样")
        out.append((doc, canonical_bytes(doc)))
    return out


def read_signatures(path: Path = SIGNATURES) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SIGNATURES_SCHEMA:
        raise SystemExit(f"{path} 的格式版本不认识")
    return dict(data.get("actions", {}))


def desktop_public_key():
    """电脑端内置的那把公钥。签完拿它验一遍：私钥和它不是一对的话，签了也白签。"""
    _hashes, serialization, _ec = load_backend()
    from motioncontrol.app_update import PUBLIC_KEY_B64
    return serialization.load_der_public_key(base64.b64decode(PUBLIC_KEY_B64))


def verify(public_key, payload: bytes, signature_b64: str) -> bool:
    hashes, _serialization, ec = load_backend()
    from cryptography.exceptions import InvalidSignature
    try:
        public_key.verify(base64.b64decode(signature_b64), payload, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


def check() -> int:
    """哪些签了、哪些没签、哪些改过之后没重签。"""
    signatures = read_signatures()
    public_key = desktop_public_key()
    bad = 0
    for doc, payload in official_documents():
        entry = signatures.get(doc["id"])
        digest = hashlib.sha256(payload).hexdigest()
        if entry is None:
            print(f"  未签名    {doc['id']}（{doc['name']}）")
            bad += 1
        elif entry.get("sha256") != digest:
            print(f"  改过没重签 {doc['id']}（{doc['name']}）")
            bad += 1
        elif not verify(public_key, payload, entry.get("signature", "")):
            print(f"  签名不对  {doc['id']}（{doc['name']}）：不是电脑端那把公钥对应的私钥签的")
            bad += 1
        else:
            print(f"  已签名    {doc['id']}（{doc['name']}）第 {doc['revision']} 版")
    print("全部可以发布" if not bad else f"{bad} 个不会发布")
    return 1 if bad else 0


def sign(private_key_path: Path) -> int:
    hashes, serialization, ec = load_backend()
    if not private_key_path.is_file():
        raise SystemExit(f"找不到私钥 {private_key_path}")
    private = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    public_key = desktop_public_key()
    actions = {}
    for doc, payload in official_documents():
        signature = base64.b64encode(private.sign(payload, ec.ECDSA(hashes.SHA256()))).decode("ascii")
        if not verify(public_key, payload, signature):
            raise SystemExit("这把私钥和电脑端内置的公钥不是一对，签了电脑端也不认。检查私钥路径。")
        actions[doc["id"]] = {
            "revision": doc["revision"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "signature": signature,
        }
        print(f"  已签名 {doc['id']}（{doc['name']}）第 {doc['revision']} 版")
    SIGNATURES.write_text(json.dumps({"schema": SIGNATURES_SCHEMA, "actions": actions},
                                     ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"写入 {SIGNATURES.relative_to(ROOT)}，一共 {len(actions)} 个动作。部署云端后生效。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="给官方动作库签名")
    parser.add_argument("--check", action="store_true", help="只检查签名状态，不需要私钥")
    parser.add_argument("--key", help="私钥路径（默认读 config/phone_web_signing_key.txt）")
    args = parser.parse_args()
    if args.check:
        return check()
    return sign(key_path(args.key))


if __name__ == "__main__":
    raise SystemExit(main())
