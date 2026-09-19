"""发布产物叫什么、放在哪，只算这一次。

三个打包脚本原来各自写死了同一串路径（build/release/电脑端/MotionControl-PC-x.y.z
这种），部署脚本里还有第四份。发版时它们必须同时改成一样的，而漏掉一个的后果是
脚本在一个不存在的目录上安静地什么都不做——stage_release 会报"0 个文件"，
build_netdisk_bundle 会说找不到，但谁也不会说"你是不是版本号改了一半"。

这里只做名字，不碰文件。版本号从 motioncontrol/version.py 来，那是全项目唯一
的那一处。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from motioncontrol.version import VERSION  # noqa: E402

PC_NAME = f"MotionControl-PC-{VERSION}"
APK_NAME = f"MotionControl-Android-{VERSION}.apk"
NETDISK_NAME = f"MotionControl-{VERSION}-完整版.zip"

RELEASE_DIR = ROOT / "build" / "release"
PC_DIR = RELEASE_DIR / "电脑端" / PC_NAME
PC_ZIP = RELEASE_DIR / "电脑端" / f"{PC_NAME}.zip"
APK = RELEASE_DIR / "手机端" / APK_NAME
NETDISK_ZIP = RELEASE_DIR / NETDISK_NAME

__all__ = [
    "VERSION", "ROOT", "RELEASE_DIR",
    "PC_NAME", "APK_NAME", "NETDISK_NAME",
    "PC_DIR", "PC_ZIP", "APK", "NETDISK_ZIP",
]


if __name__ == "__main__":
    # push.sh 和其它 shell 要拿版本号/路径时走这里，而不是自己再拼一遍。
    #     python tools/release_paths.py version
    #     python tools/release_paths.py pc_dir
    what = sys.argv[1] if len(sys.argv) > 1 else "version"
    print({
        "version": VERSION,
        "pc_dir": PC_DIR,
        "pc_zip": PC_ZIP,
        "apk": APK,
        "netdisk": NETDISK_ZIP,
    }[what])
