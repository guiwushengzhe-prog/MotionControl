"""启动 MotionControl。换包在这里做，不在 app/ 里面。

为什么需要这一层：更新要把 app/ 整个换掉，而 server.py 就住在里面。启动脚本
原来是 cd 进 app/ 再跑 server.py，于是当前工作目录就是要被换掉的那个目录——
Windows 不允许删除或改名自己所在的目录，退回时会删到一半然后失败，把安装掏空。

真撞过一次：测试里退回之后 app/ 变成空的，软件再也打不开。备份还在，但普通
用户看到的就是"打不开了"。

所以这个文件待在 app/ 外面，工作目录也在外面。它只做三件事：把下好的换进去、
需要时退回去、然后把控制权交给 app/server.py。它自己不参与更新——不能更新的
东西就该尽量少，所以这里只有这些。
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"


def main() -> int:
    if not (APP / "server.py").is_file():
        # app/ 不见了或者被掏空了：能救就救。这正是上面那个坑的兜底。
        backup = ROOT / "app_previous"
        if (backup / "server.py").is_file():
            print("程序目录不完整，正在用上一份恢复…")
            try:
                if APP.exists():
                    os.rename(APP, ROOT / "app_broken")
                os.rename(backup, APP)
            except OSError as exc:
                print(f"恢复失败：{exc}")
        if not (APP / "server.py").is_file():
            print("找不到 app 目录下的 server.py。请重新解压一次发布包。")
            return 1

    sys.path.insert(0, str(APP))
    try:
        from motioncontrol import app_update
        print("更新：", app_update.promote(APP))
    except Exception as exc:  # noqa: BLE001 - 更新出任何问题都不该拦住启动
        print(f"更新检查跳过：{exc}")

    # 交给 app/server.py。用 runpy 而不是 import，是因为它要以 __main__ 的身份
    # 跑——它底下有 if __name__ == "__main__" 的启动分支。
    os.chdir(APP)
    sys.path.insert(0, str(APP))
    sys.argv[0] = str(APP / "server.py")
    runpy.run_path(str(APP / "server.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
