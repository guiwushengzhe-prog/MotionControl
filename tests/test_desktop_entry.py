"""编译真实桌面程序并检查快捷方式；隔离安装，不注册当前用户或打开浏览器。"""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(os.name != "nt" or not PWSH, reason="Windows 原生桌面入口")


def test_installed_shortcuts_target_the_native_program_and_paths_with_spaces_work(tmp_path):
    destination, desktop, programs = (tmp_path / name for name in ("稳定入口 带空格", "桌面", "开始菜单"))
    result = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-File", str(ROOT / "安装桌面入口.ps1"),
                             "-Destination", str(destination), "-DesktopDirectory", str(desktop),
                             "-ProgramsDirectory", str(programs), "-DoNotRegister"],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    executable = destination / "MotionControl.exe"
    raw = executable.read_bytes()
    pe = struct.unpack_from("<I", raw, 0x3C)[0]
    assert raw[:2] == b"MZ" and raw[pe:pe + 4] == b"PE\0\0"
    assert struct.unpack_from("<H", raw, pe + 24 + 68)[0] == 2, "应为原生窗口程序，不能是控制台快捷方式"
    script = tmp_path / "inspect.ps1"
    script.write_text("""param($Directory)
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut((Join-Path $Directory 'MotionControl.lnk'))
@{target=$s.TargetPath; icon=$s.IconLocation; directory=$s.WorkingDirectory; arguments=$s.Arguments} | ConvertTo-Json -Compress
""", encoding="utf-8")
    for directory in (desktop, programs):
        check = subprocess.run([PWSH, "-NoProfile", "-File", str(script), str(directory)],
                               capture_output=True, text=True, encoding="utf-8", timeout=15, check=True)
        shortcut = json.loads(check.stdout)
        assert Path(shortcut["target"]) == executable
        assert Path(shortcut["directory"]) == destination
        assert shortcut["icon"] == str(executable) + ",0" and shortcut["arguments"] == ""
    moved = tmp_path / "迁移后的 程序文件夹"
    moved.mkdir()
    for name in ("START.ps1", "server.py"):
        (moved / name).write_text("test")
    report = tmp_path / "verify.txt"
    for path, expected in ((ROOT, "ready"), (moved, "ready"), (tmp_path / "missing", "missing-program")):
        checked = subprocess.run([str(executable), "--verify", str(path), str(report)], timeout=10)
        assert checked.returncode == (0 if expected == "ready" else 2)
        assert report.read_text() == expected
