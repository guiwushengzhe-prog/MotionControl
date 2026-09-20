"""版本号只许有一处。

发 2.0.1 之前它散在十一个地方：server.py、语音目录的 product_version、两条写死
"2.0.0" 的测试断言、三个打包脚本里的路径、部署脚本、README 里的 APK 文件名，还有
三个产物自己的名字。

漏改一处不会报错，只会让两个数字对不上——而反馈表单恰好让用户填"你用的版本"。
填的和跑的不是一回事，那条反馈就查不下去了。更糟的是打包脚本：路径里的版本号改了
一半，脚本会在一个不存在的目录上安静地什么也不做，报告里写着"0 个文件"，没有一句
话提示你是版本号改漏了。

所以这里扫源码，不让数字再长回去。
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motioncontrol.version import VERSION  # noqa: E402

# 只扫会被人改的地方。build/ 是产物，.git 是历史，node_modules 不是我们的。
SKIP_DIRS = {"build", ".git", "node_modules", "__pycache__", "dist",
             "app_bundle", "models", "venv", ".venv", "output"}
# 这些文件里出现版本号是它本来的工作。
ALLOWED = {
    "motioncontrol/version.py",          # 唯一那一处
    "tests/test_version_is_single_sourced.py",
    "CHANGELOG.md",                      # 更新日志当然要写版本号
    # 从 CHANGELOG.md 生成的，不是手改的地方。它和源文件同步没同步，由下面
    # test_the_website_copy_is_in_sync 管。
    "cloud/web/public/changelog.json",
}
VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\b")


def sources():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".sh", ".txt", ".json"}:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative in ALLOWED or relative.endswith("-lock.json"):
            continue
        yield relative, path


def test_the_current_version_is_not_written_down_anywhere_else():
    """写死当前版本号的地方，就是下次发版会漏改的地方。"""
    offenders = []
    for relative, path in sources():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if VERSION in line:
                offenders.append(f"{relative}:{number}: {line.strip()[:70]}")
    assert not offenders, (
        "这些地方写死了当前版本号，发下一版时会漏掉：\n  " + "\n  ".join(offenders))


def test_the_server_takes_it_from_that_one_place():
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "from motioncontrol.version import VERSION" in server
    assert not re.search(r'VERSION\s*=\s*["\']\d', server), "server.py 又自己写了一个版本号"


def test_the_release_names_are_derived(monkeypatch):
    """产物名跟着版本号走，不是手打的。"""
    import importlib

    import tools.release_paths as paths
    importlib.reload(paths)
    assert VERSION in paths.PC_NAME
    assert VERSION in paths.APK_NAME
    assert VERSION in paths.NETDISK_NAME
    assert paths.PC_DIR.name == paths.PC_NAME


@pytest.mark.parametrize("script", [
    "tools/build_netdisk_bundle.py",
    "tools/build_app_bundle.py",
])
def test_the_build_scripts_share_one_definition(script):
    text = (ROOT / script).read_text(encoding="utf-8")
    assert "release_paths" in text, f"{script} 自己拼路径，没走共用的那处"


def test_the_deploy_script_asks_instead_of_guessing():
    text = (ROOT / "cloud" / "deploy" / "push.sh").read_text(encoding="utf-8")
    assert "release_paths.py" in text


def _first_python_call(text: str) -> int:
    """第一处真正调用 python 的位置。

    不能直接找 "python" 这个词：两条护栏自己的报错信息里就写着它，找出来的位置
    会落在护栏内部，于是"护栏在调用之前"这类断言都会因为错误的理由通过。第一版
    就是这么写的，UTF-8 那条当场挂了，WSL 那条则是侥幸过的。
    """
    match = re.search(r"(?m)^[^#\n]*\bpython\s+(?:-m|tools/)", text)
    assert match, "push.sh 里找不到任何一处 python 调用"
    return match.start()


def test_the_deploy_script_reads_python_output_as_utf8():
    """发布目录名里有中文，而 Windows 的 Python 默认按控制台编码（GBK）写管道。

    不设 PYTHONIOENCODING 的话，bash 拿到的是 GBK 字节、磁盘上是 UTF-8，
    [ -d "$PC_DIR/app" ] 永远为假，电脑端更新包那一步被整个跳过。它跳过时说的是
    "没有发布包，这次不带更新包"——一句听着完全正常的话。2026-09-20 真发生过，
    线上那份自更新包因此一直没换，而部署输出里看不出任何异常。
    """
    text = (ROOT / "cloud" / "deploy" / "push.sh").read_text(encoding="utf-8")
    export = text.find("PYTHONIOENCODING")
    assert export != -1, "push.sh 没有强制 python 输出 UTF-8"
    assert export < _first_python_call(text), (
        "PYTHONIOENCODING 排在第一次调用 python 之后了，那次调用还是会拿到 GBK")


def test_the_deploy_script_refuses_wsl_before_it_fails_obscurely():
    """在 PowerShell 里敲 bash 会落到 WSL，那里既没有 python 也没有 ssh 配置。

    这一条钉的是顺序：护栏必须排在第一次用 python 之前。排在后面等于没有——
    人先看到的是 "python: command not found"，然后去查 Python 装没装，而真正的
    原因是这个 shell 不对。2026-09-20 真踩过一次。
    """
    text = (ROOT / "cloud" / "deploy" / "push.sh").read_text(encoding="utf-8")
    guard = text.find("/proc/sys/kernel/osrelease")
    assert guard != -1, "push.sh 里没有 WSL 护栏"
    assert guard < _first_python_call(text), (
        "WSL 护栏排在第一次调用 python 之后了——那时报错信息已经把人带偏")


def test_this_version_has_a_changelog_entry():
    """发了版但忘了写日志，网站上就缺一条，而缺的那条没人会发现。

    版本号现在改一个数字就全跟着走了——方便到了可以不小心发出去的程度。这一条
    是唯一会拦住"改完数字直接打包"的东西。
    """
    path = ROOT / "CHANGELOG.md"
    assert path.is_file(), "没有 CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    assert re.search(rf"^##\s+{re.escape(VERSION)}\b", text, re.M), (
        f"CHANGELOG.md 里没有 {VERSION} 这一节。发版之前先写：\n"
        f"    ## {VERSION} — YYYY-MM-DD")


def test_the_website_copy_is_in_sync():
    """网站读的是生成出来的 JSON，写完 md 忘了跑转换的话，网站上就缺这一版。

    而缺的那一版正是刚发的那一版——最需要有人看见的那一条。
    """
    import json

    from tools.build_changelog import TARGETS, parse

    expected = parse((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    for target in TARGETS:
        assert target.is_file(), f"{target.name} 还没生成：python tools/build_changelog.py"
        actual = json.loads(target.read_text(encoding="utf-8"))["releases"]
        assert actual == expected, (
            f"{target.name} 和 CHANGELOG.md 对不上，跑一遍："
            " python tools/build_changelog.py")
    # 最上面那一节可能是网页包的热更条目，它的号和电脑端不是一条线。要比的是
    # 最新的那条电脑端条目。
    newest_app = next((r for r in expected if r.get("channel", "app") == "app"), None)
    assert newest_app is not None, "CHANGELOG 里一条电脑端版本都没有"
    assert newest_app["version"] == VERSION, (
        "CHANGELOG 里最新的电脑端版本不是当前版本——新的要写在最前面")


def test_a_malformed_version_heading_is_not_silently_dropped():
    """格式写错的版本标题最坏的地方是它不报错。

    整节连同下面所有条目一起消失，md 里看着好好的，网站上就是没有——而缺的
    通常正是刚发的那一版。
    """
    from tools.build_changelog import parse

    with pytest.raises(ValueError, match="整节会被丢掉"):
        parse("## 网页版 2.0.1 — 2026-01-01\n\n- 改了点东西\n")


def test_a_web_only_entry_says_it_is_web_only():
    """网页包能独立热更，所以它有自己的版本号，和电脑端不是一条线。

    两条线都可能出到 2.0.1。解析结果不带 channel 的话，网站上两节标题一模一样，
    读的人分不清哪一节要重装、哪一节手机自己就更了。
    """
    from tools.build_changelog import parse

    releases = parse("## 网页 2.0.1 — 2026-01-01\n\n- 改了点东西\n\n"
                     "## 2.0.1 — 2026-01-01\n\n- 另一件事\n")
    assert [(r["channel"], r["version"]) for r in releases] == [
        ("web", "2.0.1"), ("app", "2.0.1")]
