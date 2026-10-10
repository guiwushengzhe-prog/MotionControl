"""Windows administrator restart for the selected game profile."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
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
    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = (("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE))

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(ShellExecuteInfo),)
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    info = ShellExecuteInfo(cbSize=ctypes.sizeof(ShellExecuteInfo),
                            fMask=0x40 | 0x100, lpVerb="runas", lpFile=executable,
                            lpParameters=parameters, lpDirectory=directory, nShow=0)
    if not shell32.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
        raise RuntimeError("没有获得管理员权限，当前程序继续运行")
    try:
        result = kernel32.WaitForSingleObject(info.hProcess, 2000)
        if result == 0:
            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
            raise RuntimeError(f"管理员后台提前退出（{code.value}），当前程序继续运行")
        if result != 0x102:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(info.hProcess)


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
