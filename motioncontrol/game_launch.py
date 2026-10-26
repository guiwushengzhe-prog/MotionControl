"""Windows administrator restart for the selected game profile."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import json
import subprocess
import sys
import tempfile
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
    if "--user-data-dir" not in args:
        from motioncontrol.user_paths import user_data_root
        args.extend(["--user-data-dir", str(user_data_root().resolve())])
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
    from .owned_process import lifetime_job_active
    if lifetime_job_active():
        # 管理员后台不能继承旧后台的退出清理组，否则旧后台一退出就把它一起结束。
        _launch_outside_lifetime_job(executable, parameters, directory)
    else:
        _launch_elevated_process(executable, parameters, directory)


def _launch_outside_lifetime_job(executable, parameters, directory):
    from .user_paths import user_data_root
    with tempfile.TemporaryDirectory(prefix=".administrator-start-", dir=user_data_root()) as folder:
        request = Path(folder) / "request.json"
        request.write_text(json.dumps({"schema": 1, "executable": executable, "parameters": parameters,
                                       "directory": directory}, ensure_ascii=False), encoding="utf-8")
        command = ([sys.executable] if getattr(sys, "frozen", False) else
                   [sys.executable, "-X", "utf8", "-m", "motioncontrol.game_launch"])
        try:
            # 此帮助进程负责跨实例交接，必须显式脱离旧组；普通设备进程不使用此标志。
            result = subprocess.run([*command, "--elevation-request", str(request)], cwd=directory,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB | subprocess.CREATE_NO_WINDOW, timeout=150)
            receipt = json.loads((Path(folder) / "result.json").read_bytes())
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("管理员交接未完成，原程序继续运行") from error
        if result.returncode or not isinstance(receipt, dict) or receipt.get("accepted") is not True:
            reason = receipt.get("error") if isinstance(receipt, dict) else ""
            raise RuntimeError(reason or "管理员交接未确认，原程序继续运行")


def complete_elevation_request(path):
    """内部帮助入口，只能启动同一解释器和程序目录，不加载设备或绑定端口。"""
    path = Path(path)
    try:
        raw = path.read_bytes()
        if len(raw) > 64 * 1024:
            raise ValueError("管理员交接请求无效")
        request = json.loads(raw)
        if (not isinstance(request, dict) or request.get("schema") != 1 or
                not isinstance(request.get("executable"), str) or
                Path(request["executable"]).resolve() != Path(sys.executable).resolve() or
                not isinstance(request.get("directory"), str) or
                Path(request["directory"]).resolve() != Path(__file__).resolve().parents[1] or
                not isinstance(request.get("parameters"), str) or not request["parameters"] or
                len(request["parameters"]) > 32767):
            raise ValueError("管理员交接请求与当前程序不相符")
        _launch_elevated_process(request["executable"], request["parameters"], request["directory"])
        receipt = {"accepted": True}
    except (OSError, RuntimeError, ValueError) as error:
        receipt = {"accepted": False, "error": str(error)}
    (path.parent / "result.json").write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    return 0 if receipt["accepted"] else 1


def _launch_elevated_process(executable, parameters, directory):
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


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--elevation-request", type=Path, required=True)
    raise SystemExit(complete_elevation_request(parser.parse_args().elevation_request))
