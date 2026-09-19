"""工作区里的 shell 脚本必须是 LF，不能是 CRLF。

.gitattributes 里已经写了 `*.sh text eol=lf`，理由也写在那儿：CRLF 的脚本在
Linux 上直接报 `bad interpreter: /usr/bin/env bash^M`。但那一行只管 git 读写的
时候，而 push.sh 打包上传的是**工作区**的文件：

    tar czf - ... cloud motioncontrol_shared game_profiles | ssh "$HOST" ...

所以只要有哪个工具把工作区里的 bootstrap.sh 写成了 CRLF——编辑器、一个用
write_bytes 的脚本、一次复制粘贴——它就会原样传到服务器上，而 .gitattributes
一点忙都帮不上：git 根本没经手。

今天就是这样发生的：改 push.sh 的时候两个文件一起变成了 CRLF，git 提交时又
悄悄转回 LF，所以仓库是干净的、diff 是干净的，只有工作区是坏的。而 bootstrap.sh
只在第一次初始化一台机器时才执行，这种问题要等到下次换服务器才会炸。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP = {"build", ".git", "node_modules", "venv", ".venv", "dist", "output"}


def shell_scripts():
    for path in sorted(ROOT.rglob("*.sh")):
        if any(part in SKIP for part in path.relative_to(ROOT).parts):
            continue
        yield path


def test_there_are_shell_scripts_to_check():
    """这个测试自己不能悄悄变成空跑。"""
    assert list(shell_scripts()), "一个 .sh 都没扫到，说明扫的方式坏了"


@pytest.mark.parametrize("path", list(shell_scripts()), ids=lambda p: p.name)
def test_no_carriage_returns(path):
    data = path.read_bytes()
    assert b"\r\n" not in data, (
        f"{path.relative_to(ROOT)} 是 CRLF。它会被原样传到 Linux 上，"
        f"然后报 bad interpreter: ...^M。转回 LF："
        f"python -c \"p=open(r'{path}','rb');d=p.read();p.close();"
        f"open(r'{path}','wb').write(d.replace(b'\\r\\n',b'\\n'))\"")
