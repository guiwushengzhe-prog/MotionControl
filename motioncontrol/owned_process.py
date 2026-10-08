"""后台启动的内部进程随后台退出；不按进程名称扫描或结束其他实例。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import subprocess
import threading
import uuid


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io_counters", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


_lock = threading.Lock()
_job_handle = None
_job_name = ""


def ensure_lifetime_job() -> str:
    """先绑定当前后台，再创建子进程，避免先启动后绑定留下时间窗口。"""
    global _job_handle, _job_name
    if os.name != "nt":
        return ""
    with _lock:
        if _job_handle is not None:
            return _job_name
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        name = "Local\\MotionControl-Lifetime-" + str(os.getpid()) + "-" + uuid.uuid4().hex
        handle = kernel32.CreateJobObjectW(None, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # 最后一个作业句柄关闭时，结束整个进程树。
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            raise error
        if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            raise error
        # 句柄不可继承，也不能在退出清理中提前关闭（作业包含后台自身）。
        # 始终只由这个后台持有；进程退出、崩溃或被强制结束时由 Windows 关闭。
        _job_handle, _job_name = handle, name
        return name


def popen(*args, **kwargs):
    ensure_lifetime_job()
    return subprocess.Popen(*args, **kwargs)


def run(*args, **kwargs):
    ensure_lifetime_job()
    return subprocess.run(*args, **kwargs)


def terminate_and_wait(process, *, timeout=3.0):
    """只结束调用方保存的子进程，并等待其真正退出。"""
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)
