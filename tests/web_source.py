"""桌面网页的脚本拆成了 web/app.js（入口）和 web/js/*.js（各功能模块）。

查源码的测试从这里一次读全部，不用关心某段代码现在在哪个文件里。
"""
from pathlib import Path


def read_web_js(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parent.parent
    web = root / "web"
    files = [web / "app.js", *sorted((web / "js").glob("*.js"))]
    return "\n".join(path.read_text(encoding="utf-8") for path in files)
