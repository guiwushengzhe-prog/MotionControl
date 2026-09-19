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
