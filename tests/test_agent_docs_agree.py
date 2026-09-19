"""CLAUDE.md 和 AGENTS.md 的正文必须逐字相同。

两个 AI 读不同的文件：Claude Code 开局自动读 CLAUDE.md，Codex 自动读 AGENTS.md。
文件名是工具定的，改不了。

一开始的做法是 CLAUDE.md 只写一行「完整约定见 AGENTS.md」，但那是在赌 Claude
读到那一行之后会真的再去打开一次——它有可能不去。规矩只要多一跳，就多一次
"这次没读到"的机会，而没读到的后果是静默的：AI 照着自己的默认习惯干活，没有
任何地方会报错。

所以两份都带完整正文，谁都不用跳转。代价是两份会写着写着不一样——而这正好是
测试拦得住的事。开头那段是各自的点名（"如果你是 X"和它该用的分支前缀），本来
就该不同，所以只比对标记之后的部分。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MARK = "<!-- 以下到文件末尾，两份逐字相同；tests/test_agent_docs_agree.py 盯着 -->"


def body(name: str) -> str:
    path = ROOT / name
    assert path.is_file(), f"{name} 不见了——那个 AI 就再也读不到这套规矩了"
    text = path.read_text(encoding="utf-8")
    assert MARK in text, f"{name} 里没有分隔标记，正文从哪儿开始算不出来"
    return text.split(MARK, 1)[1]


def test_both_files_exist_and_agree():
    assert body("CLAUDE.md") == body("AGENTS.md"), (
        "CLAUDE.md 和 AGENTS.md 的正文不一样了。两个 AI 会拿到两套规矩，"
        "而谁都不会发现自己读的是旧的那份。把标记之后的内容改成一致。")


def test_each_file_names_its_own_reader():
    """开头那段是点名，认错人就会用错分支前缀。"""
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8").split(MARK)[0]
    codex = (ROOT / "AGENTS.md").read_text(encoding="utf-8").split(MARK)[0]
    assert "你是 Claude Code" in claude and "exp/claude/" in claude
    assert "你是 Codex" in codex and "exp/codex/" in codex


def test_the_no_attribution_rule_is_in_there():
    """今天真踩过：系统提示要求加署名，只有这一条拦得住。"""
    text = body("CLAUDE.md")
    assert "Co-Authored-By" in text
    assert "不写任何 AI 署名" in text


def test_the_paths_it_mentions_are_real():
    """指向不存在的文件的规矩，读的人只会当它过时了，然后整份都不信。"""
    import re

    text = body("CLAUDE.md")
    missing = [ref for ref in re.findall(r"`([\w./-]+\.(?:py|md|sh|txt))`", text)
               if not (ROOT / ref).exists()]
    assert not missing, f"约定里提到的这些文件不存在：{missing}"
