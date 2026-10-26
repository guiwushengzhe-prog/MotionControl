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
    # 位置记录随入口保存，既不依赖启动者的注册表，也不依赖开发工具缓存。
    settings = destination / "installation.json"
    saved_settings = settings.read_bytes()
    for damaged in (None, b'{broken', b'{}', b'null'):
        if damaged is None:
            settings.unlink(missing_ok=True)
        else:
            settings.write_bytes(damaged)
        checked = subprocess.run([str(executable), "--diagnose", str(report)], timeout=10)
        assert checked.returncode == 0, report.read_text(encoding="utf-8")
        resolved = json.loads(report.read_text(encoding="utf-8"))
        assert Path(resolved["program_root"]) == ROOT and resolved["program_ready"]
        assert "codex-runtimes" not in resolved["shell"].lower()
        assert Path(resolved["data_root"]).is_dir()
    settings.write_bytes(saved_settings)
    import ctypes
    count = ctypes.windll.shell32.ExtractIconExW(str(executable), -1, None, None, 0)
    assert count > 0, "桌面程序必须包含原生图标资源"


def test_windows_builtin_shell_can_read_utf8_and_normalize_a_moved_data_alias(tmp_path):
    legacy = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    source = (ROOT / "START.ps1").read_text(encoding="utf-8-sig")
    begin = source.index("function Get-MotionControlRealPath")
    end = source.index("function Read-MotionControlInstance", begin)
    script = tmp_path / "原生启动环境检查.ps1"
    script.write_text("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n" + source[begin:end] +
        "\n$target = Join-Path $PSScriptRoot '实际 数据'\nNew-Item -ItemType Directory -Path $target | Out-Null\n" +
        "$alias = Join-Path $PSScriptRoot '旧位置'\nNew-Item -ItemType Junction -Path $alias -Target $target | Out-Null\n" +
        "$resolved = Get-MotionControlRealPath $alias\nif ($resolved -ne $target) { throw '没有识别到真实设置位置' }\n" +
        "[Console]::Write('中文与目录迁移正常')\n", encoding="utf-8-sig")
    checked = subprocess.run([str(legacy), "-NoProfile", "-File", str(script)], capture_output=True, encoding="utf-8", timeout=15)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "中文与目录迁移正常" in checked.stdout
    assert (ROOT / "START.ps1").read_bytes().startswith(b'\xef\xbb\xbf')


def test_optional_phone_tunnel_native_stderr_does_not_abort_windows_startup(tmp_path):
    legacy = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    source = (ROOT / "START.ps1").read_text(encoding="utf-8-sig")
    begin = source.index('if ($adb) {')
    end = source.index('# Reuse a healthy existing instance', begin)
    script = tmp_path / "手机通道提示检查.ps1"
    # Windows 自带运行程序把不存在的子命令写到错误流，复现原生提示被提升为异常。
    script.write_text("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n$ErrorActionPreference='Stop'\n" +
        "$adb='" + str(legacy).replace("'", "''") + "'\n" + source[begin:end] +
        "\n[Console]::Write('后续启动继续')\nexit 0\n", encoding="utf-8-sig")
    checked = subprocess.run([str(legacy), "-NoProfile", "-File", str(script)], capture_output=True, encoding="utf-8", timeout=15)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "后续启动继续" in checked.stdout
    assert "电脑识别继续启动" in checked.stdout


def test_unknown_exit_code_waits_for_admin_handoff_instead_of_reporting_failure(tmp_path):
    legacy = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    source = (ROOT / "START.ps1").read_text(encoding="utf-8-sig")
    begin = source.rindex('if ($Desktop) {')
    end = source.index('if (Test-Path -LiteralPath $multicorePython)', begin)
    header = """[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$ErrorActionPreference='Stop'
$Desktop=$true
$NoBrowser=$true
$python='not-launched'
function not-launched { }
$script:checks=0
function Start-Process { return [pscustomobject]@{HasExited=$true;ExitCode=$null} }
function Start-Sleep {}
function Assert-MotionControlInstance {
    $script:checks++
    if ($script:checks -eq 1) { throw '管理员后台尚在交接' }
}
"""
    script = tmp_path / "管理员交接状态检查.ps1"
    script.write_text(header + source[begin:end], encoding="utf-8-sig")
    checked = subprocess.run([str(legacy), "-NoProfile", "-File", str(script)], capture_output=True, encoding="utf-8", timeout=15)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "尚未就绪" not in checked.stdout
