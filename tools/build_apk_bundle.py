"""把手机端的新版 APK 做成 App 内更新用的下载包，并签名。

    python tools/build_apk_bundle.py               # 默认是发布目录里当前版本的 APK
    python tools/build_apk_bundle.py --apk <路径>  # 指定别的 APK（版本号要对得上）

产物在 build/apk_bundle/：MotionControl.apk 和 apk.json（版本号、versionCode、是不是
功能更新、这一版的更新日志）。手机先验签，再只下几 KB 的 apk.json 决定要不要下
几十 MB 的安装包；下完还会核对包名和 versionCode，系统安装器再核对 APK 签名。

签名和手机网页包、电脑端更新包是同一把钥匙、同一个工具。没签名的包手机直接拒绝，
所以这一步不是可选的。部署：bash cloud/deploy/push.sh --apk。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motioncontrol_shared.changelog import parse, release_kind  # noqa: E402
from tools.release_paths import APK, VERSION  # noqa: E402

OUT = ROOT / "build" / "apk_bundle"
# 手机端 AppUpdatePlugin / ApkUpdatePolicy 认的就是这两个名字。
APK_NAME = "MotionControl.apk"
INFO_NAME = "apk.json"
INFO_LIMIT = 64 * 1024


def version_code(version: str) -> int:
    """和手机端 app/build.gradle 同一个公式：MAJOR*10000 + MINOR*100 + PATCH。"""
    match = re.fullmatch(r"(\d{1,4})\.(\d{1,2})\.(\d{1,2})", version)
    if not match:
        raise SystemExit(f"版本号 {version!r} 不是 x.y.z")
    major, minor, patch = (int(part) for part in match.groups())
    return major * 10000 + minor * 100 + patch


def apk_info(version: str, changelog: str) -> dict:
    """apk.json：这一版的更新日志决定手机弹不弹「新版 App」说明。

    只看 APK 那一节（标题不带「网页」）。写了新增 / 变更 / 移除的算功能更新，
    手机弹一次说明；只有修复的算系统维护，手机只在「更多设置」里留一行。
    """
    release = next((item for item in parse(changelog)
                    if item["channel"] == "app" and item["version"] == version), None)
    return {
        "version_name": version,
        "version_code": version_code(version),
        "kind": release_kind(release) if release else "system",
        "sections": release["sections"] if release else [],
    }


def build(apk: Path, version: str, changelog: str) -> dict:
    if not apk.is_file() or apk.stat().st_size < 1024:
        raise SystemExit(f"找不到 APK：{apk}")
    with apk.open("rb") as stream:
        if stream.read(2) != b"PK":
            raise SystemExit(f"{apk} 不是 APK（不是 zip）")
    info = apk_info(version, changelog)
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    text = json.dumps(info, ensure_ascii=False, indent=2) + "\n"
    if len(text.encode("utf-8")) > INFO_LIMIT:
        raise SystemExit(f"{INFO_NAME} 超过 {INFO_LIMIT // 1024} KB，手机会拒绝；这一版的更新日志写短一点")
    shutil.copy2(apk, OUT / APK_NAME)
    (OUT / INFO_NAME).write_text(text, encoding="utf-8")
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description="打包并签名 App 内更新用的 APK")
    parser.add_argument("--apk", default=str(APK), help="新版 APK（默认是发布目录里当前版本的那个）")
    parser.add_argument("--version", default=VERSION, help="APK 的版本号（默认与电脑端同一个）")
    parser.add_argument("--no-sign", action="store_true",
                        help="只打包不签名（手机会拒绝没签名的包，仅用于本地试验）")
    args = parser.parse_args()

    changelog_path = ROOT / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8") if changelog_path.is_file() else ""
    info = build(Path(args.apk), args.version, changelog)
    size = (OUT / APK_NAME).stat().st_size
    print(f"{OUT}")
    print(f"  APK {info['version_name']}（versionCode {info['version_code']}），{size / 1024 / 1024:.1f} MB")
    print(f"  {'功能更新：手机会弹一次说明' if info['kind'] == 'feature' else '系统维护：手机不弹窗'}")
    if not info["sections"]:
        print(f"  CHANGELOG 里没有 ## {info['version_name']} 这一节，手机上不会有更新说明")

    if args.no_sign:
        print("  没签名 —— 手机会拒绝这一份")
        return 0
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "sign_phone_web.py"), "--bundle", str(OUT)],
        cwd=ROOT)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
