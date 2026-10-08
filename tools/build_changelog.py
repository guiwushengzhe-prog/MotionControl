"""把 CHANGELOG.md 变成网站能直接用的 JSON。

    python tools/build_changelog.py

为什么不让网页自己解析 markdown：那要给网站加一个 markdown 库，为一页静态内容
多背几十 KB，而且解析结果没法在别处复用。转成结构化数据之后，同一份东西以后还能
喂给更新通道——玩家点"更新"之前就看得见这一版改了什么，而不是更完了不知道变了啥。

也不从 GitHub Release API 拉：用这个软件的人在国内，GitHub 时通时不通，而"更新
日志打不开"会让人以为是软件坏了。

解析的是这份 CHANGELOG 自己的格式，不是通用 markdown：

    ## 1.2.3 — 2026-01-01      一个版本
    ### 装完就能用              版本下面的小节
    - **默认全走鼠标。** …      条目，可以折行

段落（不以 - 开头、也不是标题的行）留在小节的 intro 里，因为第一版那条
「第一个公开版本。」就是这样一句话。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SOURCE = ROOT / "CHANGELOG.md"
# 网站读这份生成的 JSON。电脑端更新通道直接随包带 CHANGELOG.md，运行时用同一个
# 解析器读（见 motioncontrol/update_status.py），不经过这里。
TARGETS = (
    ROOT / "cloud" / "web" / "public" / "changelog.json",
)

# 解析规则在 motioncontrol_shared.changelog：电脑端拿同一份日志告诉用户「这一版
# 更新了什么」，两边各写一份迟早会说出两种话。
from motioncontrol_shared.changelog import parse  # noqa: E402


def main() -> int:
    if not SOURCE.is_file():
        print(f"找不到 {SOURCE}")
        return 1
    releases = parse(SOURCE.read_text(encoding="utf-8"))
    if not releases:
        print("CHANGELOG.md 里一个版本都没解析出来，格式对吗？")
        return 1

    payload = json.dumps({"releases": releases}, ensure_ascii=False, indent=2) + "\n"
    for target in TARGETS:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        print(f"  {target.relative_to(ROOT)}")
    newest = releases[0]
    print(f"{len(releases)} 个版本，最新 {newest['version']}"
          f"（{sum(len(s['items']) for s in newest['sections'])} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
