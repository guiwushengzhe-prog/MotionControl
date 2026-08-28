param(
    [string]$ProjectRoot = "",
    [string]$BasePython = "F:\MotionControl\MediaPipe\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
Write-Host "MotionControl root: $ProjectRoot"

if (-not (Test-Path $BasePython)) {
    $candidate = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path $candidate) {
        $BasePython = $candidate
    } else {
        $py = Get-Command py -ErrorAction SilentlyContinue
        if ($py) {
            $BasePython = $py.Source
        } else {
            throw "找不到 Python 3.11。请用 -BasePython 指定当前 MotionControl 的 Python。"
        }
    }
}

$venvDir = Join-Path $ProjectRoot ".venv-funasr"
$asrPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $asrPython)) {
    Write-Host "Creating isolated FunASR venv: $venvDir"
    if ([System.IO.Path]::GetFileName($BasePython).ToLowerInvariant() -eq "py.exe") {
        & $BasePython -3.11 -m venv $venvDir
    } else {
        & $BasePython -m venv $venvDir
    }
}
if (-not (Test-Path $asrPython)) { throw "创建 .venv-funasr 失败" }

Write-Host "Upgrading pip in isolated ASR venv..."
& $asrPython -m pip install --upgrade pip setuptools wheel

# CPU PyTorch is deliberate: command ASR runs only after the wake word and must
# not compete with the game/vision pipeline for the RTX 4050 VRAM.
Write-Host "Installing PyTorch CPU 2.9.1..."
& $asrPython -m pip install "torch==2.9.1" --index-url https://download.pytorch.org/whl/cpu

Write-Host "Installing FunASR 1.4.2 + ModelScope 1.39.1..."
& $asrPython -m pip install "funasr==1.4.2" "modelscope==1.39.1" soundfile

Write-Host "Downloading SeACoParaformer + FSMN-VAD..."
& $asrPython (Join-Path $ProjectRoot "tools\download_funasr_command_models.py") --root $ProjectRoot

$configDir = Join-Path $ProjectRoot "config"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
$seaco = Join-Path $ProjectRoot "models\funasr\speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
$vad = Join-Path $ProjectRoot "models\funasr\speech_fsmn_vad_zh-cn-16k-common-pytorch"
Set-Content -Path (Join-Path $configDir "funasr_python_path.txt") -Value $asrPython -Encoding UTF8
Set-Content -Path (Join-Path $configDir "funasr_seaco_model_path.txt") -Value $seaco -Encoding UTF8
Set-Content -Path (Join-Path $configDir "funasr_vad_model_path.txt") -Value $vad -Encoding UTF8

Write-Host "Verifying isolated worker/model load..."
$mainPython = $BasePython
if ([System.IO.Path]::GetFileName($BasePython).ToLowerInvariant() -eq "py.exe") {
    & $BasePython -3.11 (Join-Path $ProjectRoot "tools\verify_funasr_command_asr.py") --root $ProjectRoot
} else {
    & $mainPython (Join-Path $ProjectRoot "tools\verify_funasr_command_asr.py") --root $ProjectRoot
}

Write-Host ""
Write-Host "[OK] MotionControl PC command ASR installed."
Write-Host "FunASR Python: $asrPython"
Write-Host "SeACo model:  $seaco"
Write-Host "FSMN-VAD:     $vad"
