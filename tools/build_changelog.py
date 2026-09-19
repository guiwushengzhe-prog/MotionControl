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
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "CHANGELOG.md"
# 只给网站。服务端再存一份是想过的，但现在没有任何东西读它——等更新通道
# 真要带上"这一版改了什么"的时候再加，到时候也才知道它该长什么样。
TARGETS = (
    ROOT / "cloud" / "web" / "public" / "changelog.json",
)

RELEASE_RE = re.compile(r"^##\s+(?P<version>\d+\.\d+\.\d+)\s*(?:[—\-–]\s*(?P<date>.+))?$")
SECTION_RE = re.compile(r"^###\s+(?P<title>.+)$")
ITEM_RE = re.compile(r"^-\s+(?P<text>.+)$")


def parse(text: str) -> list[dict]:
    """(version, date, sections[]) 的列表，新的在前——文件里就是这个顺序。"""
    releases: list[dict] = []
    release: dict | None = None
    section: dict | None = None

    for raw in text.splitlines():
        line = raw.rstrip()
        match = RELEASE_RE.match(line)
        if match:
            release = {"version": match["version"],
                       "date": (match["date"] or "").strip(),
                       "sections": []}
            releases.append(release)
            section = None
            continue
        if release is None:
            # 版本号之前那一段是给读者的说明，不属于任何一版。
            continue
        match = SECTION_RE.match(line)
        if match:
            section = {"title": match["title"].strip(), "intro": "", "items": []}
            release["sections"].append(section)
            continue
        match = ITEM_RE.match(line)
        if match:
            if section is None:
                # 没有小节标题就直接列条目也是合法的，给它一个无名小节。
                section = {"title": "", "intro": "", "items": []}
                release["sections"].append(section)
            section["items"].append(match["text"].strip())
            continue
        if not line.strip() or line.startswith("---") or line.startswith("|"):
            continue
        if section is not None and section["items"]:
            # 折行的条目：接在上一条后面，而不是变成一段孤立的话。
            section["items"][-1] += " " + line.strip()
        elif section is not None:
            section["intro"] = (section["intro"] + " " + line.strip()).strip()
        else:
            section = {"title": "", "intro": line.strip(), "items": []}
            release["sections"].append(section)
    return releases


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
