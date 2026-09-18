"""Sign the phone's web bundle, so only your builds can run on a phone.

The bundle travels over the local link and becomes running code. Whoever can
put a manifest in front of a phone decides what that phone executes, and on a
LAN that is not only the machine you meant -- "it came from the PC" is not an
argument, it is an assumption.

So the phone does not trust the link. It trusts one public key, compiled into
the APK, and refuses any bundle not signed by the matching private key. The
private key never enters either repository; where it does live is per-machine
and is named in config/phone_web_signing_key.txt, which is not committed.

This cannot be added later. A build that ships without verification accepts
unsigned bundles forever, and the patch that would fix it has to travel down
the very channel that is unprotected.

    python tools/sign_phone_web.py --new-key          # once, ever
    python tools/sign_phone_web.py --bundle <dir>     # every release

What is signed is the manifest digest -- which already covers every file's
path, size and hash -- plus the time it was issued. The timestamp is what stops
a downgrade: an old bundle of yours is still correctly signed, so without it a
neighbour could hand the phone last month's version and be believed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motioncontrol.model_share import ModelShare, SIGNATURE_NAME  # noqa: E402

# 私钥放哪是每台机器自己的事，所以写在配置文件里，跟 vosk 模型路径同一个做法。
# 那个文件不进仓库。
KEY_CONFIG = ROOT / "config" / "phone_web_signing_key.txt"
SKIP = ("models/", "wasm/")


def load_backend():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:  # pragma: no cover - 环境问题，不是逻辑问题
        raise SystemExit("需要 cryptography：pip install cryptography")
    return hashes, serialization, ec


def key_path(argument: str | None) -> Path:
    """命令行参数 > 环境变量 > 配置文件。"""
    configured = argument or os.environ.get("PHONE_WEB_SIGNING_KEY", "").strip().strip('"')
    if not configured and KEY_CONFIG.is_file():
        configured = KEY_CONFIG.read_text(encoding="utf-8-sig").strip().strip('"')
    if not configured:
        raise SystemExit(
            f"不知道私钥在哪。把路径写进 {KEY_CONFIG}"
            f"（参考同名 .example），或者设环境变量 PHONE_WEB_SIGNING_KEY。")
    path = Path(configured)
    return path if path.is_absolute() else ROOT / path


def generate(path: Path) -> int:
    hashes, serialization, ec = load_backend()
    if path.exists():
        raise SystemExit(f"{path} 已经存在。覆盖它等于作废所有已装手机上的信任。")
    private = ec.generate_private_key(ec.SECP256R1())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    print(f"私钥已写入 {path}")
    print()
    print("把下面这行公钥贴进手机端 BundleSignature.java 的 PUBLIC_KEY：")
    print()
    print(base64.b64encode(public).decode("ascii"))
    print()
    print("私钥不要进任何仓库、不要贴进聊天或工单。丢了就只能发新 APK 换公钥，")
    print("已装的手机在换之前收不到任何网页更新。离线备份两份。")
    return 0


def sign(bundle: Path, path: Path) -> int:
    hashes, serialization, ec = load_backend()
    if not bundle.is_dir():
        raise SystemExit(f"找不到网页包目录 {bundle}")
    if not path.is_file():
        raise SystemExit(f"找不到私钥 {path}。第一次先跑 --new-key。")
    private = serialization.load_pem_private_key(path.read_bytes(), password=None)

    share = ModelShare("phone-web", bundle, skip=SKIP)
    manifest = share.manifest()
    if not manifest["available"]:
        raise SystemExit(f"{bundle} 里没有文件")

    payload = json.dumps(
        {"digest": manifest["digest"], "issued_at": int(time.time())},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    signature = private.sign(payload, ec.ECDSA(hashes.SHA256()))

    (bundle / SIGNATURE_NAME).write_text(json.dumps({
        "payload": base64.b64encode(payload).decode("ascii"),
        "signature": base64.b64encode(signature).decode("ascii"),
    }, indent=2), encoding="utf-8")
    print(f"已签名 {len(manifest['files'])} 个文件，共 {manifest['total_bytes'] / 1024:.0f} KB")
    print(f"digest {manifest['digest']}")
    print(f"签名写入 {bundle / SIGNATURE_NAME}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--new-key", action="store_true", help="生成一对新密钥（只做一次）")
    parser.add_argument("--bundle", help="要签名的网页包目录")
    parser.add_argument("--key", help="私钥路径")
    args = parser.parse_args()
    path = key_path(args.key)
    if args.new_key:
        return generate(path)
    if not args.bundle:
        parser.error("要么 --new-key，要么 --bundle")
    return sign(Path(args.bundle), path)


if __name__ == "__main__":
    sys.exit(main())
