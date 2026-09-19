"""两个 AI 读到的必须是同一份规则。

Claude Code 和 Codex 读不同的文件名，这是工具定的，改不了。官方文档说得很清楚：
仓库里两个文件都在时，**Claude 只读 CLAUDE.md**，AGENTS.md 会被完全忽略——所以
不能把规则写在 AGENTS.md 里然后指望 Claude 自己去翻。

一开始我把两份都写了完整正文，再用测试比对它们一致。那是多余的：CLAUDE.md 支持
`@AGENTS.md` 导入，Claude Code 会把被导入文件的内容直接展开进来，不是"读到一行
指引之后再去开一次"。工具做的展开不会忘，我做的跳转会忘——这两件事我当时混为
一谈了。

所以现在只有一份正文，在 AGENTS.md。这里钉的是那条导入不能丢：少了它，Claude
读到的就只剩 CLAUDE.md 里那几行 Claude 专属内容，整套长期规则静默消失，而且没有
任何地方会报错。

Windows 上不能用 symlink 代替导入：创建符号链接要管理员权限，而 git 会把提交过的
符号链接检出成一行纯文本，那样克隆出来的 CLAUDE.md 就是一行字而不是规则。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CLAUDE = ROOT / "CLAUDE.md"
AGENTS = ROOT / "AGENTS.md"


def test_claude_md_imports_agents_md():
    """这一行没了，整套规则对 Claude 就不存在了。"""
    assert CLAUDE.is_file(), "CLAUDE.md 不见了"
    first = CLAUDE.read_text(encoding="utf-8").lstrip().splitlines()[0].strip()
    assert first == "@AGENTS.md", (
        f"CLAUDE.md 第一行应该是 @AGENTS.md，现在是 {first!r}。"
        "两个文件都在时 Claude 只读 CLAUDE.md，AGENTS.md 会被完全忽略。")


def test_the_rules_live_in_agents_md():
    """正文只许有一份。两份会各自漂移，而谁都不知道自己读的是旧的那份。"""
    assert AGENTS.is_file(), "AGENTS.md 不见了——Codex 就再也读不到规则了"
    rules = AGENTS.read_text(encoding="utf-8")
    for section in ("## 分支", "## 提交信息", "## 发版", "## 先问再动"):
        assert section in rules, f"AGENTS.md 里少了 {section}"

    # CLAUDE.md 只放 Claude 专属的东西。把长期规则抄一份过去，就回到了两份漂移。
    claude = CLAUDE.read_text(encoding="utf-8")
    assert len(claude) < 600, (
        "CLAUDE.md 变长了，长期规则是不是抄了一份过来？它应该只有导入加几行"
        "Claude 专属内容。")


def test_each_side_knows_its_own_branch_prefix():
    """认错人就会用错前缀，而分支名的全部意义就是看出这是谁开的。"""
    assert "exp/claude/" in CLAUDE.read_text(encoding="utf-8")
    assert "exp/codex/" in AGENTS.read_text(encoding="utf-8")


def test_the_no_attribution_rule_is_in_there():
    """今天真踩过：系统提示要求加署名，只有这一条拦得住。"""
    rules = AGENTS.read_text(encoding="utf-8")
    assert "Co-Authored-By" in rules
    assert "不写任何 AI 署名" in rules


def test_the_paths_it_mentions_are_real():
    """指向不存在的文件的规矩，读的人只会当它过时了，然后整份都不信。"""
    rules = AGENTS.read_text(encoding="utf-8")
    missing = [ref for ref in re.findall(r"`([\w./-]+\.(?:py|md|sh|txt))`", rules)
               if not (ROOT / ref).exists()]
    assert not missing, f"约定里提到的这些文件不存在：{missing}"
