param([switch]$Desktop)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
Set-Location $PSScriptRoot

$multicorePython = Join-Path $PSScriptRoot '.venv-multicore\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $multicorePython) { $multicorePython } else { 'python' }
$location = @(& $python -X utf8 -m motioncontrol.user_paths --locate --register 2>&1)
if ($LASTEXITCODE -eq 2 -and $Desktop) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(($location -join "`n"), '请定位原数据目录') | Out-Null
    $picker = [System.Windows.Forms.FolderBrowserDialog]::new()
    try {
        $picker.Description = '请选择迁移后的 MotionControl 原数据文件夹，里面应有游戏配置、动作或校准文件。'
        if ($picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 2 }
        $location = @(& $python -X utf8 -m motioncontrol.user_paths --remember $picker.SelectedPath 2>&1)
    } finally { $picker.Dispose() }
}
if ($LASTEXITCODE -ne 0) {
    Write-Host ($location -join "`n")
    exit 2
}
$env:MOTIONCONTROL_USER_DIR = [string]$location[-1]

function Get-MotionControlRealPath {
    param([string]$Path)
    $directory = [System.IO.DirectoryInfo]::new([System.IO.Path]::GetFullPath($Path))
    $original = $directory.FullName
    $suffix = ''
    while ($null -ne $directory -and $null -ne $directory.Parent) {
        if ([System.IO.Directory]::Exists($directory.FullName)) {
            $target = $directory.ResolveLinkTarget($true)
            if ($null -ne $target) {
                return ([System.IO.Path]::Combine($target.FullName, $suffix)).TrimEnd('\', '/')
            }
        }
        $suffix = [System.IO.Path]::Combine($directory.Name, $suffix)
        $directory = $directory.Parent
    }
    return $original.TrimEnd('\', '/')
}

function Assert-MotionControlInstance {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/health' -TimeoutSec 4
    if ($health.shutting_down) {
        throw '现有程序正在退出，请稍候再打开。'
    }
    $dataRoot = if ($env:MOTIONCONTROL_USER_DIR) { $env:MOTIONCONTROL_USER_DIR } else { Join-Path $env:LOCALAPPDATA 'MotionControl' }
    if ($health.app -ne 'MotionControl' -or
            $health.program_root -ne (Get-MotionControlRealPath $PSScriptRoot) -or
            $health.user_data_root -ne (Get-MotionControlRealPath $dataRoot)) {
        throw "正在运行的程序位置或数据目录不同：$($health.program_root)；$($health.user_data_root)。请从对应位置打开，或先退出那份程序。"
    }
    if ($health.configuration.state -ne 'ready') {
        throw "现有程序的配置尚未恢复：$($health.configuration.error)"
    }
}

function Wait-MotionControlAdministrator {
    param([string]$GameId, [int]$TimeoutSeconds = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $selected = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/game-profiles/selected' -TimeoutSec 2
            # The previous ordinary instance may still answer during handoff.
            if ($selected.profile.selected_id -eq $GameId -and
                $selected.launch.game_id -eq $GameId -and $selected.launch.is_admin -eq $true) {
                $cameraState = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/kernel/status' -TimeoutSec 2
                if ($cameraState.body_mode -in @('computer', 'phone') -and
                        $null -ne $cameraState.camera -and $null -ne $cameraState.kernel) {
                    Assert-MotionControlInstance
                    return $cameraState
                }
            }
        } catch {
            # Connection refusals are expected while the old instance exits.
        }
        Start-Sleep -Milliseconds 500
    }
    throw '管理员程序未能在一分钟内就绪，未打开页面或另起后台。请查看启动窗口中的错误后重试。'
}

# If an authorized Android phone is already connected, expose the local
# MotionControl service to the phone as 127.0.0.1:8765. This is intentionally
# best-effort: desktop camera mode must still start when ADB is unavailable.
# The tunnel works the same over USB and over wireless debugging, and 127.0.0.1
# stays correct no matter how the Wi-Fi address changes.
$adbCandidates = @(
    'F:\Android\platform-tools\adb.exe',
    (Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe')
)
$adb = $adbCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($adb) {
    # Split on the tab that "adb devices" puts between serial and state rather
    # than on whitespace: a wireless serial is an mDNS name that can itself
    # contain a space, e.g. "adb-XXXX-yyyy (2)._adb-tls-connect._tcp".
    $authorizedDevice = & $adb devices 2>$null |
        Where-Object { $_ -match "`t" -and ($_ -split "`t")[1].Trim() -eq 'device' } |
        Select-Object -First 1
    if ($authorizedDevice) {
        $serial = ($authorizedDevice -split "`t")[0]
        & $adb -s $serial reverse tcp:8765 tcp:8765 | Out-Null
        Write-Host "手机通道已连接：127.0.0.1:8765（$serial）"
    }
}

# Reuse a healthy existing instance instead of starting a second process that
# would fail while binding 8765/8766.
try {
    $running = Invoke-WebRequest -Uri 'http://127.0.0.1:8766/' -TimeoutSec 2
} catch {
    $running = $null
}
if ($running -and $running.StatusCode -eq 200) {
    try {
        Assert-MotionControlInstance
        $selected = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/game-profiles/selected' -TimeoutSec 2
    } catch {
        Write-Host "现有程序未能通过运行核对，未打开页面或另起后台：$($_.Exception.Message)"
        exit 1
    }
    $cameraState = $null
    $gameId = [string]$selected.profile.selected_id
    if (-not $gameId -or $selected.launch.game_id -ne $gameId) {
        Write-Host '现有程序的游戏设置尚未就绪，未打开页面或另起后台。请稍后重试。'
        exit 1
    }
    if ($selected.launch.requires_admin -and -not $selected.launch.is_admin) {
        $restart = $null
        try {
            $restart = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/game-profiles/launch-mode' `
                -Method Post -ContentType 'application/json' -Body '{"admin":true}' -TimeoutSec 120
            if ($restart.ok -ne $true) { throw '程序未接受管理员重启请求。' }
        } catch {
            $restart = $null
            Write-Host '未能切换为管理员，正在核实原程序是否仍在运行。'
            try {
                $selected = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/game-profiles/selected' -TimeoutSec 2
                if ($selected.profile.selected_id -ne $gameId -or $selected.launch.game_id -ne $gameId) {
                    throw '原游戏的程序尚未就绪。'
                }
                $cameraState = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/kernel/status' -TimeoutSec 3
                if ($cameraState.body_mode -notin @('computer', 'phone') -or
                        $null -eq $cameraState.camera -or $null -eq $cameraState.kernel) {
                    throw '原程序未能返回识别状态。'
                }
                if (-not $selected.launch.is_admin) {
                    Write-Host '原程序仍以普通权限运行；本次不再请求管理员权限。'
                }
            } catch {
                Write-Host '原程序也未响应，未打开页面或另起后台。请查看启动窗口中的错误后重试。'
                exit 1
            }
        }
        if ($null -ne $restart) {
            if ($restart.restarting -eq $true) {
                Write-Host '已请求按当前游戏的设置以管理员方式重启，正在等待新程序就绪。'
            } else {
                Write-Host '正在核实当前游戏的管理员程序是否就绪。'
            }
            try {
                $cameraState = Wait-MotionControlAdministrator -GameId $gameId
            } catch {
                Write-Host $_.Exception.Message
                exit 1
            }
            Write-Host '当前游戏的管理员程序已就绪。'
        }
    }
    try {
        if ($null -eq $cameraState) {
            $cameraState = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/kernel/status' -TimeoutSec 3
        }
        if ($cameraState.body_mode -eq 'computer' -and -not $cameraState.camera.running) {
            Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/input/source' `
                -Method Post -ContentType 'application/json' -Body '{"source":"computer","enabled":true}' -TimeoutSec 55 | Out-Null
        }
    } catch {
        Write-Host "摄像头未能自动启动，可在首页画面下方重试识别：$($_.Exception.Message)"
        try {
            Invoke-RestMethod -Uri 'http://127.0.0.1:8766/api/game-profiles/selected' -TimeoutSec 2 | Out-Null
        } catch {
            Write-Host '程序已失去响应，未打开页面或另起后台。请查看启动窗口中的错误后重试。'
            exit 1
        }
    }
    Write-Host 'MotionControl 已在运行，正在打开控制页面。'
    Start-Process 'http://127.0.0.1:8766/'
    exit 0
}

if ($Desktop) {
    $logs = Join-Path $env:MOTIONCONTROL_USER_DIR 'logs'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $service = Start-Process -FilePath $python -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
        -ArgumentList @('-X', 'utf8', 'server.py', '--no-browser') `
        -RedirectStandardOutput (Join-Path $logs 'startup.stdout.log') `
        -RedirectStandardError (Join-Path $logs 'startup.stderr.log')
    $deadline = (Get-Date).AddSeconds(75)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        try { Assert-MotionControlInstance; $ready = $true; break } catch {}
        # 普通进程可能已成功交接给管理员进程，不能把正常退出当作启动失败。
        if ($service.HasExited -and $service.ExitCode -ne 0) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        Write-Host "程序尚未就绪，请查看 $logs 中的启动记录。"
        exit 1
    }
    Start-Process 'http://127.0.0.1:8766/'
    exit 0
}

if (Test-Path -LiteralPath $multicorePython) {
    & $multicorePython -X utf8 server.py
} else {
    python -X utf8 server.py
}
exit $LASTEXITCODE
