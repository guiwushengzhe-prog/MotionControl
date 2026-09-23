"""Stage the Android half of the release, with the checks that were missing.

The 2.0 bundle's phone folder was assembled by hand, and it showed: the staged
``MotionControl-手机端-2.0.apk`` was signed ``C=US, O=Android, CN=Android
Debug``. Everything the release-signing work achieved was sitting in
``F:\\switch\\output`` while the bundle carried the debug build, under a
filename that did not match the one the PC instructions tell people to look
for. Shipping it would have shipped a debuggable app signed with a key that is
on every developer's machine on earth.

A hand-assembled step is why that happened, so the fix is not to copy the right
file this once. It is to make staging refuse the wrong one:

* the signature must verify, and must not be the Android debug key
* applicationId, versionCode and versionName are read out of the APK itself --
  not out of build.gradle, which describes what the next build would produce
* the APK must not be marked debuggable
* the filename is derived from the version, so it cannot drift from the
  instructions again

Every check is fatal. A missing apksigner is fatal too: "could not check" and
"checked and it is fine" must not produce the same outcome, which is exactly
the trap BUILD_MOBILE_RELEASE.cmd falls into when it warns and carries on.

    python tools/stage_mobile.py --target "build/release/手机端"
    python tools/stage_mobile.py --target ... --check      # report, change nothing
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.release_paths import APK_NAME, VERSION  # noqa: E402

# Frozen at 2.0 and not to be changed casually: the applicationId is the app's
# permanent identity on every device that installs it, and versionCode may only
# ever go up.
EXPECTED_APPLICATION_ID = "cn.motioncontrol.app"
# 手机端和电脑端同发一个版本号，所以核对的就是那一处。以前这里写死一个数字，
# 发下一版时它会把一个正确的 APK 当成版本不对拒掉。
EXPECTED_VERSION_NAME = VERSION
# versionCode 只能往上走，而且要是整数：1.2.3 -> 10203。
EXPECTED_VERSION_CODE = sum(
    int(part) * scale for part, scale in zip(VERSION.split("."), (10000, 100, 1)))

DEFAULT_APK = Path(r"F:\switch\output") / APK_NAME
DEBUG_SIGNER_MARKER = "CN=Android Debug"


def _build_tools() -> Path:
    """The newest installed build-tools directory."""
    local = os.environ.get("LOCALAPPDATA", "")
    root = Path(os.environ.get("ANDROID_SDK_ROOT") or Path(local) / "Android" / "Sdk")
    candidates = sorted((root / "build-tools").glob("*"), key=lambda p: p.name)
    if not candidates:
        raise SystemExit(
            f"找不到 Android build-tools（找过 {root / 'build-tools'}）。"
            "签名和版本都无法核对，拒绝打包。")
    return candidates[-1]


def _run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise SystemExit(f"{Path(command[0]).name} 执行失败：\n{result.stderr.strip()}")
    return result.stdout


def apk_identity(apk: Path, build_tools: Path) -> dict:
    """applicationId / versionCode / versionName / debuggable, read from the APK."""
    badging = _run([str(build_tools / "aapt2.exe"), "dump", "badging", str(apk)])
    package = re.search(
        r"package: name='([^']+)' versionCode='(\d+)' versionName='([^']*)'", badging)
    if package is None:
        raise SystemExit("aapt2 读不出包信息，这个文件可能不是有效的 APK")
    return {
        "application_id": package.group(1),
        "version_code": int(package.group(2)),
        "version_name": package.group(3),
        # aapt2 only prints this line when the flag is actually set.
        "debuggable": "application-debuggable" in badging,
    }


def apk_signer(apk: Path, build_tools: Path) -> str:
    output = _run([str(build_tools / "apksigner.bat"), "verify", "--print-certs", str(apk)])
    signer = re.search(r"Signer #1 certificate DN: (.+)", output)
    if signer is None:
        raise SystemExit("apksigner 没有报告签名者，这个 APK 可能未签名")
    return signer.group(1).strip()


def check(apk: Path) -> tuple[dict, str]:
    """Every gate. Raises SystemExit on the first failure, with the reason."""
    if not apk.is_file():
        raise SystemExit(f"找不到 APK：{apk}\n先运行 F:\\switch\\BUILD_MOBILE_RELEASE.cmd")

    build_tools = _build_tools()
    signer = apk_signer(apk, build_tools)
    identity = apk_identity(apk, build_tools)

    if DEBUG_SIGNER_MARKER in signer:
        raise SystemExit(
            f"这个 APK 是用 Android 调试密钥签的，不能发布。\n"
            f"  签名者：{signer}\n"
            f"  调试密钥在每台装过 Android SDK 的机器上都一样，任何人都能用它冒充更新。\n"
            f"  用 BUILD_MOBILE_RELEASE.cmd 重新构建（它会读 keystore.properties）。")
    if identity["debuggable"]:
        raise SystemExit("这个 APK 标了 android:debuggable，是 debug variant 的产物，不能发布。")

    problems = []
    if identity["application_id"] != EXPECTED_APPLICATION_ID:
        problems.append(f"applicationId 是 {identity['application_id']}，"
                        f"应为 {EXPECTED_APPLICATION_ID}")
    if identity["version_name"] != EXPECTED_VERSION_NAME:
        problems.append(f"versionName 是 {identity['version_name']}，"
                        f"应为 {EXPECTED_VERSION_NAME}")
    if identity["version_code"] != EXPECTED_VERSION_CODE:
        problems.append(f"versionCode 是 {identity['version_code']}，"
                        f"应为 {EXPECTED_VERSION_CODE}")
    if problems:
        raise SystemExit("APK 身份和本次发布对不上：\n  " + "\n  ".join(problems))

    return identity, signer


def readme_text(filename: str) -> str:
    """The phone-side instructions.

    Regenerated rather than copied, because a hand-kept copy goes stale.
    """
    return f"""MotionControl 2.0 手机端

把 {filename} 传到安卓手机上安装。
安装时如果提示“未知来源”，按提示允许即可。

用法：
  1. 电脑那边先启动服务（另一个文件夹里的 启动.bat）。
  2. 手机和电脑连同一个 Wi-Fi，或者插数据线打开「USB 网络共享」。
  3. 打开手机应用，点「连接并开始」，它会自己找到电脑。

手机第一次打开会要摄像头和麦克风权限，都要给。
姿态识别和语音识别都在手机本地完成，不上传画面和声音。

同一个网络里的设备都能连上这台电脑，传输内容也不加密。
只在自己家里的网络里用。
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="手机端发布目录")
    parser.add_argument("--apk", default=str(DEFAULT_APK), help="已签名的 release APK")
    parser.add_argument("--check", action="store_true", help="只检查，不改动")
    args = parser.parse_args()

    apk = Path(args.apk)
    identity, signer = check(apk)

    filename = f"MotionControl-Android-{identity['version_name']}.apk"
    target = Path(args.target)
    destination = target / filename

    print(f"APK       {apk}")
    print(f"  身份    {identity['application_id']} "
          f"{identity['version_name']} ({identity['version_code']})")
    print(f"  签名    {signer}")
    print(f"  可调试  否")
    print(f"目标      {destination}")

    # Anything else in the folder is from an earlier build under an earlier
    # name, and leaving it there is how someone installs the wrong one.
    strays = [path for path in target.glob("*.apk") if path.name != filename] \
        if target.is_dir() else []
    for path in strays:
        print(f"  移除    {path.name}（旧的或改过名的构建产物）")

    if args.check:
        current = destination.is_file() and destination.read_bytes() == apk.read_bytes()
        return 0 if current and not strays else 1

    target.mkdir(parents=True, exist_ok=True)
    for path in strays:
        path.unlink()
    shutil.copy2(apk, destination)
    (target / "请先看.txt").write_text(readme_text(filename), encoding="utf-8")
    print("\n已打包。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
