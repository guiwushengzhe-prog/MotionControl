"""把电脑端、手机端和指南打成一个压缩包，用来发网盘。

GitHub Release 上是三个独立文件，那适合会用 GitHub 的人。发网盘的对象不是——
让他分别下三个再自己想清楚哪个是哪个，中间任何一步断了他就放弃了。

所以这里只出一个文件：解压一次，三样东西都在，各自放在名字看得懂的文件夹里。
电脑端是解压好的，不是嵌套压缩包——套娃压缩包要解两次，而第二次很多人不会做。

    python tools/build_netdisk_bundle.py
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.release_paths import APK, NETDISK_ZIP as OUT, PC_DIR, ROOT, VERSION  # noqa: E402

GUIDE = ROOT / "release" / "新手指南.pdf"

TOP_README = f"""MotionControl {VERSION}
体感游戏控制器 —— 用普通摄像头把身体动作变成游戏操作


这个压缩包里有什么
────────────────────

  新手指南.pdf     先看这个。图文教程，十分钟能玩起来
  电脑端\\          主程序，必须装
  手机端\\          可选，只在笔记本摄像头拍不全你时才需要


最短路径
────────

  1. 打开「电脑端」文件夹，双击「安装虚拟手柄驱动.exe」
     这一步不能跳。不装的话你做动作时界面上区域会亮，但游戏里没反应。

  2. 双击「启动.bat」，黑框不要关，浏览器打开 http://127.0.0.1:8766

  3. 跟着网页右边的「开始前检查」一步步走


两件事先说清楚
──────────────

  这是站着玩的，需要大约两米见方的地方，摄像头要能拍到你的头和双肩。

  内置约 200 个游戏配置，绝大多数是自动生成的、没有人真的玩过，界面上标着
  「实验配置」。不好用是正常的，可以自己改。


遇到问题、想要功能，直接跟我说
──────────────────────────────

  QQ 群    1101605483

    装不上、连不上、动作不认，群里问最快，也能看到别人怎么解决的。
    调好了某个游戏的映射也欢迎发群里 —— 内置那两百个绝大多数没人真玩过。

  网页反馈  https://motioncontrol.guiwu-aware.icu/feedback

    不想进群就用这个，不用注册，打开就能写。留个联系方式我才回得了你。
    这个网站上还能下载和分享别人调好的游戏配置。

  源码      https://github.com/guiwushengzhe-prog/MotionControl

本软件按 AGPL-3.0 授权，完整条文见「电脑端」文件夹里的 LICENSE。
"""


def main() -> int:
    missing = [p for p in (PC_DIR, APK, GUIDE) if not p.exists()]
    if missing:
        for p in missing:
            print(f"缺少 {p}")
        raise SystemExit("先把发布物准备好再打包")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    root = f"MotionControl-{VERSION}"
    files = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{root}/请先看.txt", TOP_README.replace("\n", "\r\n"))
        z.write(GUIDE, f"{root}/新手指南.pdf")
        z.write(APK, f"{root}/手机端/{APK.name}")
        files += 3
        for path in sorted(PC_DIR.rglob("*")):
            if path.is_file():
                z.write(path, f"{root}/电脑端/{path.relative_to(PC_DIR).as_posix()}")
                files += 1

    size = OUT.stat().st_size
    digest = hashlib.sha256(OUT.read_bytes()).hexdigest()
    print(f"{OUT}")
    print(f"  {files} 个文件，{size / 1048576:.1f} MB")
    print(f"  sha256 {digest}")
    print()
    print("解压后长这样：")
    print(f"  {root}/请先看.txt")
    print(f"  {root}/新手指南.pdf")
    print(f"  {root}/电脑端/启动.bat …")
    print(f"  {root}/手机端/{APK.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
