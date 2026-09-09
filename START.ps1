param(
    [int]$Port = 8765,
    [string]$ListenAddress = '0.0.0.0',
    [switch]$NoBrowser,
    [switch]$NoAutoStart
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
Set-Location -LiteralPath $PSScriptRoot

$baseUrl = "http://127.0.0.1:$Port"
$openUrl = "$baseUrl/"
$serverProcess = $null

function Invoke-LocalApi {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [ValidateSet('GET', 'POST')][string]$Method = 'GET',
        [object]$Body = $null,
        [int]$TimeoutSec = 10
    )
    $request = @{ Uri = "$baseUrl$Path"; Method = $Method; TimeoutSec = $TimeoutSec }
    if ($null -ne $Body) {
        $request.ContentType = 'application/json'
        $request.Body = $Body | ConvertTo-Json -Compress -Depth 8
    }
    Invoke-RestMethod @request
}

function Connect-PhoneUsb {
    $adbCandidates = @(
        'F:\Android\platform-tools\adb.exe',
        (Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe')
    )
    $adb = $adbCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $adb) {
        Write-Host '未找到手机调试工具；电脑摄像头仍可启动。' -ForegroundColor DarkYellow
        return
    }
    $device = & $adb devices 2>$null |
        Select-String '^[^\s]+\s+device(?:\s|$)' |
        Select-Object -First 1
    if (-not $device) {
        Write-Host '没有发现已授权手机；电脑摄像头仍可启动。' -ForegroundColor DarkYellow
        return
    }
    $serial = ($device.Line -split '\s+')[0]
    & $adb -s $serial reverse "tcp:$Port" "tcp:$Port" | Out-Null
    Write-Host "手机 USB 通道已连接：ws://127.0.0.1:$Port/ws/input" -ForegroundColor Green
}

function Get-ExistingMotionControl {
    try { return (Invoke-LocalApi -Path '/api/input/status') }
    catch { return $null }
}

function Wait-MotionControl {
    param([System.Diagnostics.Process]$Process)
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        if ($Process -and $Process.HasExited) {
            throw "MotionControl 服务提前退出，日志：$env:TEMP\MotionControl-$Port.err.log"
        }
        try { return (Invoke-LocalApi -Path '/api/input/status') }
        catch { Start-Sleep -Milliseconds 250 }
    }
    throw "MotionControl 服务在 20 秒内没有监听 $Port 端口。日志：$env:TEMP\MotionControl-$Port.err.log"
}

function Start-ComputerCamera {
    if ($NoAutoStart) {
        Write-Host '已跳过自动启动电脑摄像头（-NoAutoStart）。' -ForegroundColor DarkYellow
        return
    }
    try {
        $result = Invoke-LocalApi -Path '/api/input/source' -Method POST -Body @{ source = 'computer'; enabled = $true } -TimeoutSec 60
        $camera = $result.camera
        if ($camera.running) {
            $width = $camera.resolution.width
            $height = $camera.resolution.height
            Write-Host "电脑摄像头已启动：${width}x${height}" -ForegroundColor Green
        }
        else {
            Write-Warning "服务已启动，但电脑摄像头没有进入运行状态：$($camera.last_error)"
        }
    }
    catch {
        Write-Warning ("自动启动电脑摄像头失败：{0}。网页仍会打开，可点击开始体感重试。" -f $_.Exception.Message)
    }
}

Connect-PhoneUsb

$existing = Get-ExistingMotionControl
if ($existing) {
    Write-Host "检测到已有 MotionControl 服务：$baseUrl" -ForegroundColor Cyan
    if ([string]$existing.body_mode -eq 'phone' -or $existing.mobile_pose_connected) {
        Write-Host '已有服务正在使用手机身体源，启动脚本不会强制切换到电脑摄像头。' -ForegroundColor DarkYellow
    }
    else {
        Start-ComputerCamera
    }
    if (-not $NoBrowser) { Start-Process $openUrl }
    Write-Host "网页：$openUrl"
    return
}

$pythonCandidates = @(
    'F:\MotionControl\MediaPipe\.venv\Scripts\python.exe',
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Get-Command python -ErrorAction SilentlyContinue).Source
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
$python = $pythonCandidates | Select-Object -First 1
if (-not $python) {
    throw '没有找到可用的 Python。请确认 F:\MotionControl\MediaPipe\.venv 存在。'
}
$probe = & $python -c "import mediapipe, cv2, numpy, sounddevice" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Python 环境缺少电脑摄像头依赖：$python`n$probe"
}
$pythonVersion = & $python --version 2>&1
Write-Host "使用正式 Python 环境：$python（$pythonVersion）" -ForegroundColor DarkGray
$stdoutLog = Join-Path $env:TEMP "MotionControl-$Port.out.log"
$stderrLog = Join-Path $env:TEMP "MotionControl-$Port.err.log"
$arguments = @('-X', 'utf8', 'server.py', '--host', $ListenAddress, '--port', [string]$Port, '--no-browser')
$serverProcess = Start-Process -FilePath $python `
    -ArgumentList $arguments `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

try {
    Wait-MotionControl -Process $serverProcess | Out-Null
    Write-Host "MotionControl 服务已启动：$openUrl" -ForegroundColor Cyan
    Start-ComputerCamera
    if (-not $NoBrowser) { Start-Process $openUrl }
    Write-Host "网页：$openUrl"
    Write-Host '关闭此窗口会停止本次服务；输出仍保持关闭，需在网页中主动开启。' -ForegroundColor DarkGray
    Wait-Process -Id $serverProcess.Id
}
finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
