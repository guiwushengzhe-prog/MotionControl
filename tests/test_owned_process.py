"""使用真实 Windows 子进程验证归属和退出，不启动摄像头或实际识别。"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows 作业对象验证")


def _open_process(pid):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x00100000, False, pid)
    assert handle, f"测试子进程尚未建立：{pid}"
    return api, handle


def _owner(code):
    return subprocess.Popen([sys.executable, "-X", "utf8", "-u", "-c", code],
                            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            creationflags=subprocess.CREATE_NO_WINDOW)


def _pid_line(owner):
    result = []
    reader = threading.Thread(target=lambda: result.append(owner.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout=5)
    if reader.is_alive():
        owner.kill()
        owner.wait(timeout=5)
        reader.join(timeout=5)
        pytest.fail("测试进程没有及时报告子进程编号")
    return result[0].strip()


@pytest.mark.parametrize("exit_mode", ["normal", "forced", "crash"])
def test_backend_exit_ends_its_children_and_grandchildren_only(exit_mode):
    # 另一个实例即使使用同一个解释器，也不属于被关闭后台的作业。
    external = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    code = (
        "import os, subprocess, sys\n"
        "from motioncontrol.owned_process import ensure_lifetime_job\n"
        "ensure_lifetime_job()\n"
        # 直接创建也应继承归属，覆盖以后新增的内部子进程。
        f"child = subprocess.Popen([sys.executable, '-u', '-c', {worker!r}], stdout=subprocess.PIPE, text=True)\n"
        "grandchild = int(child.stdout.readline())\n"
        "print(str(child.pid) + ' ' + str(grandchild), flush=True)\n"
        "sys.stdin.readline()\n"
        + ("os._exit(23)\n" if exit_mode == "crash" else "")
    )
    owner = _owner(code)
    handles = []
    try:
        pids = _pid_line(owner).split()
        assert len(pids) == 2, "未收到子进程和后代编号"
        handles = [_open_process(int(pid)) for pid in pids]
        if exit_mode == "forced":
            owner.kill()
        else:
            owner.stdin.write("exit\n")
            owner.stdin.flush()
        owner.wait(timeout=5)
        for api, handle in handles:
            assert api.WaitForSingleObject(handle, 5000) == 0, "后台退出后仍有残留子进程"
        assert external.poll() is None, "关闭后台影响了其他实例"
        if exit_mode == "normal":
            assert owner.returncode == 0
    finally:
        for api, handle in handles:
            api.CloseHandle(handle)
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)
        external.terminate()
        external.wait(timeout=5)


@pytest.mark.parametrize("launch", ["popen", "run"])
def test_blocked_helper_is_bound_before_it_starts(launch):
    child = (
        "import os, sys\n"
        "print(os.getpid(), flush=True)\n"
        "while True: sys.stdout.buffer.write(b'x' * 1048576); sys.stdout.flush()\n"
    )
    code = (
        "import sys, time, subprocess\n"
        f"from motioncontrol.owned_process import {launch}\n"
        f"process = {launch}([sys.executable, '-u', '-c', {child!r}], stdout=sys.stdout, stderr=sys.stderr, creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "time.sleep(60)\n"
    )
    owner = _owner(code)
    api = handle = None
    try:
        pid = _pid_line(owner)
        assert pid.isdecimal(), "未收到测试子进程编号"
        api, handle = _open_process(int(pid))
        # 模拟输出管道无人读取、采集进程无法自行处理停止信号。
        owner.kill()
        owner.wait(timeout=5)
        assert api.WaitForSingleObject(handle, 5000) == 0
    finally:
        if handle:
            api.CloseHandle(handle)
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)


@pytest.mark.parametrize("backend_alive", [True, False])
def test_broker_started_helper_joins_existing_backend_or_cancels(tmp_path, backend_alive):
    # 只执行配置脚本的归属绑定部分，不检查或修改防火墙，也不请求提权。
    source = (ROOT / "motioncontrol/windows_firewall.ps1").read_text(encoding="utf-8-sig")
    binding = source.split("[Console]::OutputEncoding", 1)[0]
    script = tmp_path / "bind-lifetime.ps1"
    script.write_text(binding + "\n[Console]::WriteLine('bound')\n[Threading.Thread]::Sleep(60000)\n",
                      encoding="utf-8-sig")
    owner = _owner("import sys\nfrom motioncontrol.owned_process import ensure_lifetime_job\n"
                   "print(ensure_lifetime_job(), flush=True)\nsys.stdin.readline()\n")
    helper = None
    try:
        name = _pid_line(owner)
        assert name.startswith("Local\\MotionControl-Lifetime-")
        if not backend_alive:
            owner.stdin.write("exit\n")
            owner.stdin.flush()
            owner.wait(timeout=5)
        # 从测试父进程启动，模拟通过 Windows 提权代理创建的独立助手。
        shell = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        helper = subprocess.Popen([str(shell), "-NoProfile", "-NonInteractive", "-File", str(script),
                                   "-Program", sys.executable, "-LifetimeJob", name],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", errors="replace",
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        if backend_alive:
            assert _pid_line(helper) == "bound", helper.communicate(timeout=5)[1]
            owner.stdin.write("exit\n")
            owner.stdin.flush()
            owner.wait(timeout=5)
            helper.wait(timeout=5)
        else:
            output, error = helper.communicate(timeout=10)
            assert helper.returncode != 0 and "bound" not in output and "Bind" in error
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)
        if helper is not None:
            if helper.poll() is None:
                helper.kill()
            helper.communicate(timeout=5)


def test_kinect_release_can_repeat_and_reaps_only_its_process(monkeypatch):
    from motioncontrol import kinect_camera, owned_process
    code = "import time; time.sleep(60)"
    real_popen = owned_process.popen
    monkeypatch.setattr(kinect_camera, "bridge_executable", lambda: "test-kinect")
    monkeypatch.setattr(owned_process, "popen", lambda *_args, **kwargs:
                        real_popen([sys.executable, "-c", code], **kwargs))
    camera = kinect_camera.KinectCapture("kinect2:test")
    try:
        camera.release()
        camera.release()
        assert camera._process.poll() is not None
        assert not camera._reader.is_alive()
        assert camera._process.stdout.closed and camera._process.stderr.closed
    finally:
        camera.release()


def test_kinect_start_failure_closes_its_already_started_process(monkeypatch):
    from motioncontrol import kinect_camera, owned_process
    processes = []
    real_popen = owned_process.popen
    def started(*_args, **kwargs):
        process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(kinect_camera, "bridge_executable", lambda: "test-kinect")
    monkeypatch.setattr(owned_process, "popen", started)
    monkeypatch.setattr(kinect_camera.threading.Thread, "start", lambda _self:
                        (_ for _ in ()).throw(RuntimeError("测试线程启动失败")))
    try:
        with pytest.raises(RuntimeError, match="测试线程启动失败"):
            kinect_camera.KinectCapture("kinect2:test")
        assert len(processes) == 1 and processes[0].poll() is not None
        assert processes[0].stdout.closed and processes[0].stderr.closed
    finally:
        for process in processes:
            owned_process.terminate_and_wait(process)


@pytest.mark.parametrize("ignores_close", [False, True])
def test_optional_voice_worker_has_single_owner_and_bounded_close(tmp_path, monkeypatch, ignores_close):
    from motioncontrol import owned_process
    from motioncontrol.funasr_command_backend import FunAsrWorkerClient
    processes = []
    real_popen = owned_process.popen
    def launched(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(owned_process, "popen", launched)
    worker = tmp_path / "tools/funasr_command_worker.py"
    worker.parent.mkdir()
    worker.write_text(
        "import json, sys\nprint(json.dumps({'type': 'ready'}), flush=True)\n"
        "for line in sys.stdin:\n"
        + ("    pass\n" if ignores_close else "    if json.loads(line).get('type') == 'close': break\n"),
        encoding="utf-8")
    client = FunAsrWorkerClient(tmp_path, Path(sys.executable), tmp_path, tmp_path)
    try:
        starts = [threading.Thread(target=client.start) for _ in range(2)]
        for thread in starts:
            thread.start()
        for thread in starts:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert client._ready_event.wait(5) and client.ready
        assert len(processes) == 1
        process = client.process
        client.start()
        assert client.process is process
        client.close()
        client.close()
        assert process.poll() is not None and client.process is None
        assert process.stdin.closed and process.stdout.closed
        assert not client._reader_thread.is_alive()
        assert client._stderr_handle is None and not client.ready
    finally:
        client.close()
        for process in processes:
            owned_process.terminate_and_wait(process)
