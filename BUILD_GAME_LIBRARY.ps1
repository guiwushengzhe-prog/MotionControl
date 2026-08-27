param(
    [int]$SeedCount = 500,
    [int]$MaxPages = 30,
    [int]$CommunityLimit = 25,
    [int]$CheckpointEvery = 10,
    [double]$DelaySeconds = 0.12,
    [switch]$RefreshExisting,
    [string]$SeedFile = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Resolve-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return [pscustomobject]@{ Exe = $py.Source; Prefix = @("-3") } }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return [pscustomobject]@{ Exe = $python.Source; Prefix = @() } }
    throw "Python 3 not found. Install Python or add py/python to PATH."
}

function Invoke-Python($PythonCmd, [string[]]$Arguments) {
    $exe = $PythonCmd.Exe
    $prefix = @($PythonCmd.Prefix)
    & $exe @prefix @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $exe $($Arguments -join ' ')"
    }
}

$PythonCmd = Resolve-Python
if ([string]::IsNullOrWhiteSpace($SeedFile)) {
    $SeedFile = Join-Path $Root "game_profiles\seeds_catalog.json"
    Write-Host "[1/3] Building Steam seed catalog (target only: $SeedCount)..."
    Invoke-Python $PythonCmd @(
        "tools\build_seed_catalog.py",
        "--output", $SeedFile,
        "--count", "$SeedCount",
        "--max-pages", "$MaxPages"
    )
} else {
    if (-not [System.IO.Path]::IsPathRooted($SeedFile)) { $SeedFile = Join-Path $Root $SeedFile }
    if (-not (Test-Path $SeedFile)) { throw "Seed file not found: $SeedFile" }
    Write-Host "[1/3] Using existing seed file: $SeedFile"
}

Write-Host "[2/3] Building/updating offline Game Profiles..."
$BuildArgs = @(
    "tools\build_game_profiles.py", $SeedFile,
    "--root", $Root,
    "--community-limit", "$CommunityLimit",
    "--checkpoint-every", "$CheckpointEvery",
    "--delay", "$DelaySeconds"
)
if ($RefreshExisting) { $BuildArgs += "--refresh-existing" }
Invoke-Python $PythonCmd $BuildArgs

Write-Host "[3/3] Auditing library (report only; never a profile-count gate)..."
Invoke-Python $PythonCmd @("tools\audit_game_profiles.py", "--root", $Root)

Write-Host ""
Write-Host "Done. Runtime library: game_profiles\catalog.json"
Write-Host "Build report:     game_profiles\build_run_report.json"
Write-Host "Audit report:     game_profiles\audit_report.json"
Write-Host "No minimum Profile count is enforced."
