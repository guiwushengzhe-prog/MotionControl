"""把新手指南转成一个自带图片的 HTML，放进发布包。

为什么不是直接放 markdown：Windows 上双击 .md 不会渲染，普通用户看到的是一堆
`![...](...)` 原文，图片一张也看不见。

为什么不是只放 GitHub 链接：GitHub 的图片全部从 raw.githubusercontent.com 取，
那个域名在中国大陆经常连不上——页面能开、文字正常、图片全裂。而这份指南的读者
恰恰就是刚下载完、还没装上、最需要看图的人。

所以图片用 base64 内嵌进 HTML。一个文件，双击就看，断网也能看。

    python tools/build_guide_html.py
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "新手指南.md"
# HTML 只是转 PDF 的中间产物，不进发布包——包里放两份一模一样的东西，只会让
# 打开的人犹豫该点哪个。它落在 build/ 里，方便改样式时直接在浏览器里看效果。
TARGET = ROOT / "build" / "新手指南.html"
PDF = ROOT / "release" / "新手指南.pdf"

# 源文件的 sha256 写进 HTML，打包时对一次。改了 markdown 忘了重新生成，
# 发布包里就会是旧指南——而这种错没人会发现，直到用户照着旧步骤做不通。
STAMP = "mc-guide-source-sha256"

STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 40px 20px 80px;
  background: #0d1117; color: #c9d1d9;
  font: 16px/1.8 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
}
main { max-width: 820px; margin: 0 auto; }
h1 { font-size: 30px; margin: 0 0 8px; color: #f0f6fc; }
h2 { font-size: 22px; margin: 44px 0 14px; padding-bottom: 8px;
     border-bottom: 1px solid #21262d; color: #f0f6fc; }
h3 { font-size: 17px; margin: 28px 0 10px; color: #f0f6fc; }
p, li { margin: 10px 0; }
a { color: #58a6ff; }
strong { color: #f0f6fc; }
code {
  background: #161b22; border: 1px solid #30363d; border-radius: 6px;
  padding: 1px 6px; font-size: 14px;
  font-family: ui-monospace, Consolas, monospace; color: #e6edf3;
}
pre {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 14px 16px; overflow-x: auto;
}
pre code { background: none; border: 0; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 15px; }
th, td { border: 1px solid #30363d; padding: 9px 12px; text-align: left;
         vertical-align: top; }
/* 不换行，否则「必须吗」这种短表头会被压成一列一个字。 */
th { background: #161b22; color: #f0f6fc; white-space: nowrap; }
/* 最后一列通常是说明文字，让它吃掉剩下的宽度。 */
td:last-child, th:last-child { width: 99%; }
img {
  max-width: 100%; height: auto; display: block;
  margin: 18px 0; border: 1px solid #30363d; border-radius: 8px;
}
blockquote {
  margin: 16px 0; padding: 10px 16px;
  border-left: 3px solid #3fb950; background: #12261a; border-radius: 0 6px 6px 0;
}
blockquote p { margin: 4px 0; }
hr { border: 0; border-top: 1px solid #21262d; margin: 36px 0; }
.foot { margin-top: 56px; padding-top: 16px; border-top: 1px solid #21262d;
        font-size: 13px; color: #8b949e; }
/* PDF 走的是打印样式。只翻 body 的底色不够——标题、表头、代码本来是浅色字，
   在白底上会变成白底白字，整页看着是空的。所以这里把每一处都翻过来。 */
@media print {
  body { background: #fff; color: #1a1a1a; padding: 0; }
  h1, h2, h3, strong, th { color: #000; }
  h2 { border-bottom-color: #ccc; }
  a { color: #0645ad; }
  code { background: #f4f4f4; border-color: #ddd; color: #1a1a1a; }
  pre { background: #f8f8f8; border-color: #ddd; }
  th { background: #f0f0f0; }
  th, td { border-color: #bbb; }
  img { border-color: #ccc; page-break-inside: avoid; }
  blockquote { background: #f1f8f2; border-left-color: #2a7; }
  hr { border-top-color: #ddd; }
  .foot { border-top-color: #ddd; color: #555; }
  /* 标题不要落在页面最后一行，图不要被切成两半。 */
  h1, h2, h3 { page-break-after: avoid; }
}
@page { margin: 16mm 14mm; }
"""


def inline_images(html: str, base: Path) -> tuple[str, int]:
    """把 <img src="images/x.png"> 换成 base64，让这个文件能离线看。"""
    count = 0

    def swap(match: re.Match) -> str:
        nonlocal count
        src = match.group(2)
        if src.startswith(("http://", "https://", "data:")):
            return match.group(0)
        path = (base / src).resolve()
        if not path.is_file():
            raise SystemExit(f"图片找不到：{path}（指南里写的是 {src}）")
        kind = mimetypes.guess_type(path.name)[0] or "image/png"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        count += 1
        return f'{match.group(1)}data:{kind};base64,{data}{match.group(3)}'

    return re.sub(r'(<img[^>]*\ssrc=")([^"]+)(")', swap, html), count


def main() -> int:
    try:
        import markdown
    except ImportError:
        raise SystemExit("需要 markdown：pip install markdown")

    if not SOURCE.is_file():
        raise SystemExit(f"找不到指南源文件 {SOURCE}")
    text = SOURCE.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    body, n = inline_images(body, SOURCE.parent)

    title = "MotionControl 新手指南"
    html = (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta name="{STAMP}" content="{digest}">\n'
        f"<title>{title}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n<main>\n"
        + body
        + '\n<div class="foot">MotionControl 2.0 · AGPL-3.0 · '
          '这份文件的图片已内嵌，断网也能看。'
          '最新版见 <a href="https://github.com/guiwushengzhe-prog/MotionControl">项目主页</a>。</div>\n'
        "</main>\n</body>\n</html>\n"
    )

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(html, encoding="utf-8")
    print(f"{TARGET}")
    print(f"  内嵌 {n} 张图片，{TARGET.stat().st_size / 1024:.0f} KB")
    print(f"  源文件 sha256 {digest[:16]}…")

    print()
    print(build_pdf() or "  跳过 PDF")
    return 0


def build_pdf() -> str:
    """再出一份 PDF。

    手机上打开 PDF 是一件不用教的事：系统自带阅读器，图在文件里，不联网也不用
    找浏览器。HTML 虽然也自包含，但在手机上得先想办法用浏览器打开它。

    用 Chrome 无头模式转现成的那份 HTML，不另外引一套排版库——图片已经内嵌，
    Chrome 走的是上面那段 @media print，所以 PDF 是白底黑字，能看也能打印。
    """
    chrome = next((Path(p) for p in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ) if Path(p).is_file()), None)
    if chrome is None:
        return "  找不到 Chrome 或 Edge，PDF 没生成（HTML 不受影响）"

    import subprocess
    import tempfile

    pdf = PDF
    pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        result = subprocess.run(
            [str(chrome), "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={profile}", "--no-pdf-header-footer",
             f"--print-to-pdf={pdf}", TARGET.as_uri()],
            capture_output=True, timeout=180)
    if not pdf.is_file() or pdf.stat().st_size < 10_000:
        tail = result.stderr.decode("utf-8", "ignore").strip().splitlines()[-2:]
        return "  PDF 生成失败：" + " / ".join(tail)
    return f"{pdf}" + chr(10) + f"  {pdf.stat().st_size / 1024:.0f} KB"


if __name__ == "__main__":
    sys.exit(main())
