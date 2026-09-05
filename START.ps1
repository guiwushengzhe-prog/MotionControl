$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
Set-Location $PSScriptRoot

# If an authorized Android phone is already connected, expose the local
# MotionControl service to the phone as 127.0.0.1:8765. This is intentionally
# best-effort: desktop camera mode must still start when ADB is unavailable.
$adbCandidates = @(
    'F:\Android\platform-tools\adb.exe',
    (Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe')
)
$adb = $adbCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($adb) {
    $authorizedDevice = & $adb devices 2>$null |
        Select-String '^[^\s]+\s+device(?:\s|$)' |
        Select-Object -First 1
    if ($authorizedDevice) {
        $serial = ($authorizedDevice.Line -split '\s+')[0]
        & $adb -s $serial reverse tcp:8765 tcp:8765 | Out-Null
        Write-Host "手机 USB 通道已连接：127.0.0.1:8765"
    }
}

python -X utf8 server.py
