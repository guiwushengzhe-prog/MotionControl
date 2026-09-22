$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
Set-Location $PSScriptRoot

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
    Write-Host 'MotionControl 已在运行，正在打开控制页面。'
    Start-Process 'http://127.0.0.1:8766/'
    exit 0
}

python -X utf8 server.py
