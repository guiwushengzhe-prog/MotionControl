param(
  [string]$ProjectRoot = "F:\MotionControl-App",
  [string]$PythonExe = "F:\MotionControl\MediaPipe\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$ModelName = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
$ModelUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$ModelName.tar.bz2"
$ModelsRoot = Join-Path $ProjectRoot "models"
$ModelDir = Join-Path $ModelsRoot $ModelName
$ConfigDir = Join-Path $ProjectRoot "config"
$Archive = Join-Path $env:TEMP "$ModelName.tar.bz2"

if (-not (Test-Path $PythonExe)) { throw "Python 不存在：$PythonExe" }
New-Item -ItemType Directory -Force -Path $ModelsRoot | Out-Null
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null

Write-Host "[1/4] Install sherpa-onnx 1.13.5"
& $PythonExe -m pip install "sherpa-onnx==1.13.5" "sherpa-onnx-bin==1.13.5"
if ($LASTEXITCODE -ne 0) { throw "pip 安装 sherpa-onnx 失败" }

if (-not (Test-Path $ModelDir)) {
  Write-Host "[2/4] Download official KWS model"
  Write-Host $ModelUrl
  Invoke-WebRequest -Uri $ModelUrl -OutFile $Archive
  Write-Host "[3/4] Extract model"
  tar -xf $Archive -C $ModelsRoot
  if ($LASTEXITCODE -ne 0) { throw "模型解压失败" }
  Remove-Item $Archive -Force -ErrorAction SilentlyContinue
} else {
  Write-Host "[2/4] Model directory already exists; skip download"
  Write-Host "[3/4] Skip extraction"
}

$Required = @(
  "encoder-epoch-13-avg-2-chunk-16-left-64.onnx",
  "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
  "joiner-epoch-13-avg-2-chunk-16-left-64.onnx",
  "tokens.txt",
  "en.phone"
)
foreach ($name in $Required) {
  $p = Join-Path $ModelDir $name
  if (-not (Test-Path $p)) { throw "模型文件缺失：$p" }
}

Set-Content -Path (Join-Path $ConfigDir "sherpa_kws_model_path.txt") -Value $ModelDir -Encoding UTF8

Write-Host "[4/4] Verify Python package"
& $PythonExe -c "import sherpa_onnx; print('sherpa_onnx=', sherpa_onnx.__file__); print('version=', getattr(sherpa_onnx, '__version__', getattr(sherpa_onnx, 'version', 'unknown')))"
if ($LASTEXITCODE -ne 0) { throw "sherpa-onnx 导入失败" }

Write-Host "OK"
Write-Host "Model: $ModelDir"
Write-Host "Config: $(Join-Path $ConfigDir 'sherpa_kws_model_path.txt')"
