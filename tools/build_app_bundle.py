"""把电脑端的程序本体做成一份可下发的更新包，并签名。

    python tools/build_app_bundle.py            # 默认就是当前版本的发布包

内容就是发布包里的 app/ 目录，减去属于某一份安装的路径文件——那些指向
../models、../native/ViGEmClient.dll，跟着更新包走的话会把每台机器的模型位置
覆盖成打包这台机器的。电脑端换包时会从旧的那份搬过来（见 app_update.KEEP_FROM_OLD）。

签名和手机端网页包用同一把钥匙、同一个工具。没签名的包电脑端会直接拒绝，所以
这一步不是可选的。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motioncontrol.app_update import KEEP_FROM_OLD  # noqa: E402
from tools.release_paths import PC_DIR  # noqa: E402

OUT = ROOT / "build" / "app_bundle"


def restage(target: Path, phone_web: str | None = None) -> None:
    """先把发布包对齐仓库，再拿它打更新包。

    第一次做这个包时就踩了：改完 server.py 没重新 stage，打出来的更新包里是旧
    代码，装上去之后启动确认那一段根本不存在，包再也不会被确认下来。而这种错
    不会报任何东西——更新"成功"了，只是装的是旧的。

    所以不靠人记得，工具自己先跑一遍。
    """
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "stage_release.py"),
         "--target", str(target)]
        + (["--phone-web", phone_web] if phone_web else []),
        cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit("发布包没能对齐仓库，不打更新包")


def build(app_dir: Path) -> tuple[int, int]:
    if not (app_dir / "server.py").is_file():
        raise SystemExit(f"{app_dir} 看着不像发布包里的 app/（没有 server.py）")

    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)

    skip = set(KEEP_FROM_OLD)
    files = 0
    total = 0
    for source in sorted(app_dir.rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        relative = source.relative_to(app_dir).as_posix()
        if relative in skip:
            continue
        target = OUT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files += 1
        total += source.stat().st_size
    return files, total


def main() -> int:
    parser = argparse.ArgumentParser(description="打包并签名电脑端更新包")
    parser.add_argument("--target", default=str(PC_DIR),
                        help="发布包目录")
    # stage_release 默认去 ../switch/mobile/dist 拿网页包。同一个仓库开了多个工作树
    # 之后那个默认值就是个陷阱：它指向的是**别人那份**构建产物，而这里没有任何东西
    # 会说不对——打出来的包能签名、能安装，只是手机上跑的是别人分支的网页。
    parser.add_argument("--phone-web", default=None,
                        help="手机网页包的构建产物目录（多工作树时必须显式指定）")
    parser.add_argument("--no-restage", action="store_true",
                        help="跳过对齐仓库这一步（只有你刚 stage 过才用）")
    parser.add_argument("--no-sign", action="store_true",
                        help="只打包不签名（电脑端会拒绝没签名的包，仅用于本地试验）")
    args = parser.parse_args()

    target = Path(args.target)
    if not args.no_restage:
        restage(target, args.phone_web)
    # phone_web 现在在 app/ 里，于是它跟着这份更新包发给每一台电脑，再由电脑发给
    # 手机。手机只认签名：这里带出去一份没签名或签名过期的，所有手机都会安静地
    # 拒绝，而电脑端这边一切正常，没有任何地方会说话。重新 stage 会在内容变了的
    # 时候丢掉旧签名，所以这一步必须排在 restage 之后、打包之前。
    from tools.stage_release import check_phone_web_signature
    verdict = check_phone_web_signature(target / "app" / "phone_web")
    if verdict != "签名有效":
        raise SystemExit(f"更新包里的手机网页包{verdict}")

    files, total = build(target / "app")
    print(f"{OUT}")
    print(f"  {files} 个文件，{total / 1024:.0f} KB")
    for relative in KEEP_FROM_OLD:
        if (target / "app" / relative).is_file():
            print(f"  留给各机器自己的：{relative}")

    if args.no_sign:
        print("  没签名 —— 电脑端会拒绝这一份")
        return 0
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "sign_phone_web.py"), "--bundle", str(OUT)],
        cwd=ROOT)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
