param(
    [string]$TrialPython = 'F:\VoiceRobustnessV1-trial\Scripts\python.exe'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Test-Path -LiteralPath $TrialPython)) {
    throw "Trial Python was not found: $TrialPython. Install only into an isolated location; see README.md."
}

& pwsh.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'generate_tts.ps1')
if ($LASTEXITCODE -ne 0) { throw "TTS generation failed with exit code $LASTEXITCODE" }

& $TrialPython (Join-Path $root 'run_experiment.py') --repo 'F:\MotionControl-App' --root $root
if ($LASTEXITCODE -ne 0) { throw "Offline voice comparison failed with exit code $LASTEXITCODE" }
