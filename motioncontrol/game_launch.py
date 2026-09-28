"""Windows administrator restart for the selected game profile."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path


def is_administrator() -> bool:
    if os.name != "nt":
        return False
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def elevated_command(root: Path, arguments=(), *, wait_for_pid: int = 0,
                     no_browser: bool = False) -> tuple[str, str, str]:
    """Use the same installation and interpreter; never launch arbitrary paths."""
    root = Path(root).resolve()
    args = list(arguments)
    if "--wait-for-pid" in args:
        index = args.index("--wait-for-pid")
        del args[index:index + 2]
    if no_browser and "--no-browser" not in args:
        args.append("--no-browser")
    if wait_for_pid:
        args.extend(["--wait-for-pid", str(wait_for_pid)])
    if getattr(sys, "frozen", False):
        command = args
    else:
        command = ["-X", "utf8", str(root / "server.py"), *args]
    return sys.executable, subprocess.list2cmdline(command), str(root)


def launch_as_administrator(root: Path, arguments=(), *, wait_for_pid: int = 0,
                            no_browser: bool = False) -> None:
    if os.name != "nt":
        raise RuntimeError("管理员重启只支持 Windows")
    executable, parameters, directory = elevated_command(
        root, arguments, wait_for_pid=wait_for_pid, no_browser=no_browser)
    # The operating system presents its normal consent prompt. SW_HIDE only
    # hides the Python console after approval; the consent prompt stays visible.
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    result = shell32.ShellExecuteW(None, "runas", executable, parameters, directory, 0)
    if not result or result <= 32:
        raise RuntimeError("没有获得管理员权限，当前程序继续运行")


def wait_for_previous_process(pid: int, timeout_ms: int = 30000) -> bool:
    if not pid:
        return True
    if os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True  # The old process has already exited.
    try:
        return kernel32.WaitForSingleObject(handle, timeout_ms) == 0
    finally:
        kernel32.CloseHandle(handle)
