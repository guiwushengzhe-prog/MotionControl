"""Run the real PowerShell reuse path with isolated network/process stubs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PWSH is None, reason="需要 PowerShell 7")

HARNESS = r"""
$ErrorActionPreference = 'Stop'
$scenario = $env:MC_START_TEST_SCENARIO | ConvertFrom-Json
$global:mcStartTest = @{
    calls = @($scenario.calls); index = 0; elapsed = 0
    events = [System.Collections.Generic.List[object]]::new()
}
if ($scenario.inherited_restart) { $global:restart = @{ restarting = $true } }
function Test-Path { return $false }
function Get-Date { return [datetime]::new(2020, 1, 1).AddSeconds($global:mcStartTest.elapsed) }
function Start-Sleep {
    param($Milliseconds)
    $global:mcStartTest.elapsed += 10
}
function Write-Host {
    param([Parameter(ValueFromRemainingArguments)] [object[]]$Object)
    $global:mcStartTest.events.Add(@{ kind = 'message'; text = ($Object -join ' ') })
}
function Start-Process {
    param($FilePath)
    $global:mcStartTest.events.Add(@{ kind = 'browser'; path = $FilePath })
}
function python { throw '测试不得启动新后台' }
function Invoke-WebRequest {
    param($Uri, $TimeoutSec)
    if ($Uri -ne 'http://127.0.0.1:8766/') { throw '测试请求地址不正确' }
    return [pscustomobject]@{ StatusCode = 200 }
}
function Invoke-RestMethod {
    param($Uri, $Method = 'Get', $TimeoutSec, $ContentType, $Body)
    $path = ([uri]$Uri).AbsolutePath
    $global:mcStartTest.events.Add(@{ kind = 'api'; path = $path; method = $Method })
    if ($global:mcStartTest.index -ge $global:mcStartTest.calls.Count) {
        $global:mcStartTest.events.Add(@{ kind = 'unexpected'; path = $path })
        throw '测试请求超出预期'
    }
    $next = $global:mcStartTest.calls[$global:mcStartTest.index]
    $global:mcStartTest.index += 1
    if ($path -ne $next.path -or $Method -ne $next.method) {
        $global:mcStartTest.events.Add(@{ kind = 'unexpected'; path = $path })
        throw '测试请求顺序不正确'
    }
    if ($next.error) { throw $next.error }
    return $next.value
}
$global:LASTEXITCODE = 0
try {
    & $env:MC_START_TEST_SCRIPT
    $resultCode = $LASTEXITCODE
} catch {
    $global:mcStartTest.events.Add(@{ kind = 'uncaught'; text = $_.Exception.Message })
    $resultCode = 1
}
[Console]::Out.WriteLine('MC_START_RESULT=' + (@{
    code = $resultCode
    consumed = $global:mcStartTest.index
    events = @($global:mcStartTest.events)
} | ConvertTo-Json -Depth 20 -Compress))
"""


def selected(*, game="test-game", admin=False, required=True):
    return {"profile": {"selected_id": game}, "launch": {
        "game_id": game, "is_admin": admin, "requires_admin": required,
    }}


def camera(*, running=False, source="computer"):
    return {"body_mode": source, "camera": {"running": running}, "kernel": {}}


def call(path, value=None, *, method="Get", error=None):
    return {"path": "/api/" + path, "method": method, "value": value, "error": error}


def launch(value=None, *, error=None):
    return call("game-profiles/launch-mode", value, method="Post", error=error)


def run_start(tmp_path, calls, *, inherited_restart=False):
    harness = tmp_path / "startup_harness.ps1"
    harness.write_text(HARNESS, encoding="utf-8")
    env = os.environ.copy()
    env.update(MC_START_TEST_SCRIPT=str(ROOT / "START.ps1"),
               MC_START_TEST_SCENARIO=json.dumps({"calls": calls, "inherited_restart": inherited_restart}),
               LOCALAPPDATA=str(tmp_path))
    result = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-File", str(harness)],
                            capture_output=True, text=True, encoding="utf-8", env=env, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith("MC_START_RESULT=")]
    assert len(lines) == 1, result.stdout + result.stderr
    data = json.loads(lines[0].partition("=")[2])
    assert data["consumed"] == len(calls), data
    assert not any(event["kind"] in {"uncaught", "unexpected"} for event in data["events"]), data
    data["messages"] = "\n".join(event["text"] for event in data["events"] if event["kind"] == "message")
    data["browser"] = [event for event in data["events"] if event["kind"] == "browser"]
    return data


def test_waits_for_same_game_administrator_and_kernel_before_starting_camera(tmp_path):
    data = run_start(tmp_path, [
        call("game-profiles/selected", selected()),
        launch({"ok": True, "restarting": True}),
        call("game-profiles/selected", selected()),  # Old instance still answers.
        call("game-profiles/selected", selected(game="another-game", admin=True)),
        call("game-profiles/selected", selected(admin=True)),
        call("kernel/status", error="新内核尚未响应"),
        call("game-profiles/selected", selected(admin=True)),
        call("kernel/status", camera()),
        call("input/source", {"ok": True}, method="Post"),
    ])
    assert data["code"] == 0 and len(data["browser"]) == 1
    assert "当前游戏的管理员程序已就绪" in data["messages"]


@pytest.mark.parametrize("restarting", [True, False])
def test_accepted_request_without_ready_administrator_times_out_without_opening_or_relaunching(tmp_path, restarting):
    data = run_start(tmp_path, [
        call("game-profiles/selected", selected()),
        launch({"ok": True, "restarting": restarting}),
        *[call("game-profiles/selected", selected()) for _ in range(6)],
    ])
    assert data["code"] == 1 and not data["browser"]
    assert "管理员程序未能" in data["messages"]
    assert "已就绪" not in data["messages"] and "正在打开" not in data["messages"]


def test_declined_elevation_reuses_only_healthy_original_and_ignores_inherited_restart(tmp_path):
    data = run_start(tmp_path, [
        call("game-profiles/selected", selected()),
        launch(error="用户拒绝管理员权限"),
        call("game-profiles/selected", selected()),
        call("kernel/status", camera()),
        call("input/source", {"ok": True}, method="Post"),
    ], inherited_restart=True)
    assert data["code"] == 0 and len(data["browser"]) == 1
    assert "原程序仍以普通权限运行" in data["messages"]
    assert "管理员程序已就绪" not in data["messages"]


@pytest.mark.parametrize("returned", [False, True])
def test_failed_or_unaccepted_elevation_with_missing_original_does_not_open(tmp_path, returned):
    request = launch({"ok": False, "restarting": True}) if returned else launch(error="拒绝提权")
    data = run_start(tmp_path, [
        call("game-profiles/selected", selected()), request,
        call("game-profiles/selected", error="原程序也已退出"),
    ])
    assert data["code"] == 1 and not data["browser"]


def test_declined_elevation_does_not_reuse_another_game(tmp_path):
    data = run_start(tmp_path, [
        call("game-profiles/selected", selected()), launch(error="拒绝提权"),
        call("game-profiles/selected", selected(game="another-game")),
    ])
    assert data["code"] == 1 and not data["browser"]


def test_declined_elevation_requires_healthy_kernel(tmp_path):
    data = run_start(tmp_path, [
        call("game-profiles/selected", selected()), launch(error="拒绝提权"),
        call("game-profiles/selected", selected()), call("kernel/status", {}),
    ])
    assert data["code"] == 1 and not data["browser"]


def test_wait_does_not_accept_an_empty_kernel_reply(tmp_path):
    data = run_start(tmp_path, [
        call("game-profiles/selected", selected()), launch({"ok": True, "restarting": True}),
        call("game-profiles/selected", selected(admin=True)), call("kernel/status", {}),
        call("game-profiles/selected", selected(admin=True)), call("kernel/status", camera(running=True)),
    ])
    assert data["code"] == 0 and len(data["browser"]) == 1


@pytest.mark.parametrize("source,running", [("computer", True), ("phone", False)])
def test_healthy_existing_source_opens_once_without_restarting_camera(tmp_path, source, running):
    data = run_start(tmp_path, [
        call("game-profiles/selected", selected(required=False)),
        call("kernel/status", camera(running=running, source=source)),
    ])
    assert data["code"] == 0 and len(data["browser"]) == 1


@pytest.mark.parametrize("alive", [False, True])
def test_camera_failure_opens_only_if_program_still_responds(tmp_path, alive):
    data = run_start(tmp_path, [
        call("game-profiles/selected", selected(required=False)),
        call("kernel/status", camera()), call("input/source", method="Post", error="摄像头启动失败"),
        call("game-profiles/selected", selected() if alive else None,
             error=None if alive else "程序已经退出"),
    ])
    assert data["code"] == (0 if alive else 1)
    assert len(data["browser"]) == int(alive)
    assert "摄像头未能自动启动" in data["messages"]
