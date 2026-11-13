<#
.SYNOPSIS
    Runs the HR-Onboarding-Common and UC05 test suites on Windows PowerShell.

.DESCRIPTION
    The .venv folders inside the UC01/UC04/UC05 projects were created on another machine and do not
    start here. This script builds working environments for them OUTSIDE the UC project folders
    (default: %LOCALAPPDATA%\hr-onboarding-envs), from each project's own uv.lock, so no UC directory
    is modified. It then points the orchestrator's workers at them (UC01_WORKER_PYTHON,
    UC04_WORKER_PYTHON, UC05_WORKER_PYTHON), reinstalls the current HR-Onboarding-Common into the
    UC05 environment, and runs both suites.

    Environments that already work are reused; -Rebuild recreates them.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File HR-Onboarding-Common\scripts\run_tests.ps1

.EXAMPLE
    .\HR-Onboarding-Common\scripts\run_tests.ps1 -Suite uc05 -Rebuild
#>
param(
    [string]$EnvRoot = (Join-Path $env:LOCALAPPDATA "hr-onboarding-envs"),
    [ValidateSet("all", "common", "uc05")]
    [string]$Suite = "all",
    [switch]$Rebuild
)

$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$commonDir = Join-Path $workspace "HR-Onboarding-Common"
$uc05Dir = Join-Path $workspace "UC05_Orientation-Schedule-Agent"

# name -> project folder and the uv sync arguments its pyproject needs
$workers = [ordered]@{
    uc01 = @{ Dir = "UC01_Employee_Record_Creation"; Sync = @("--group", "dev") }
    uc04 = @{ Dir = "UC04_Equipment_Access_Integration_Ready"; Sync = @() }
    uc05 = @{ Dir = "UC05_Orientation-Schedule-Agent"; Sync = @("--group", "dev") }
}

function Test-Python([string]$Python) {
    if (-not (Test-Path $Python)) { return $false }
    try {
        & $Python -c "import sys" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "'$Exe $($Arguments -join ' ')' failed with exit code $LASTEXITCODE" }
}

function Sync-Environment([string]$ProjectDir, [string]$EnvDir, [string[]]$SyncArgs) {
    if ($Rebuild -and (Test-Path $EnvDir)) { Remove-Item -Recurse -Force $EnvDir }
    Write-Host "Building $EnvDir from $ProjectDir\uv.lock ..." -ForegroundColor Cyan
    $env:UV_PROJECT_ENVIRONMENT = $EnvDir
    Push-Location $ProjectDir
    try {
        # --quiet: uv reports progress on stderr, which Windows PowerShell shows as NativeCommandError
        # when output is redirected; real errors are still printed
        Invoke-Checked "uv" (@("sync", "--frozen", "--quiet", "--python", "3.12") + $SyncArgs)
    } finally {
        Pop-Location
        Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is not on PATH; install it from https://docs.astral.sh/uv/ first"
}
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue  # an activated venv would redirect uv

# HR-Onboarding-Common keeps its environment in its own .venv (this project owns it).
$commonPython = Join-Path $commonDir ".venv\Scripts\python.exe"
if ($Rebuild -or -not (Test-Python $commonPython)) {
    Sync-Environment $commonDir (Join-Path $commonDir ".venv") @("--group", "dev")
}

# The UC worker environments live outside the UC project folders.
foreach ($name in $workers.Keys) {
    $envDir = Join-Path $EnvRoot $name
    $python = Join-Path $envDir "Scripts\python.exe"
    if ($Rebuild -or -not (Test-Python $python)) {
        Sync-Environment (Join-Path $workspace $workers[$name].Dir) $envDir $workers[$name].Sync
    }
    Set-Item -Path "Env:$($name.ToUpper())_WORKER_PYTHON" -Value $python
    Write-Host "$($name.ToUpper())_WORKER_PYTHON = $python"
}
$uc05Python = $env:UC05_WORKER_PYTHON

# UC05 installs HR-Onboarding-Common as a (non-editable) path dependency: refresh it so UC05 tests
# run against the current Common source.
Invoke-Checked "uv" @("pip", "install", "--python", $uc05Python, "--reinstall", "--no-deps", "--quiet", $commonDir)

$results = [ordered]@{}
if ($Suite -in @("all", "common")) {
    Write-Host "`n=== HR-Onboarding-Common ===" -ForegroundColor Cyan
    Push-Location $commonDir
    try { & $commonPython -m pytest; $results["HR-Onboarding-Common"] = $LASTEXITCODE } finally { Pop-Location }
}
if ($Suite -in @("all", "uc05")) {
    Write-Host "`n=== UC05_Orientation-Schedule-Agent ===" -ForegroundColor Cyan
    Push-Location $uc05Dir
    try { & $uc05Python -m pytest; $results["UC05"] = $LASTEXITCODE } finally { Pop-Location }
}

Write-Host ""
$failed = $false
foreach ($suiteName in $results.Keys) {
    $ok = $results[$suiteName] -eq 0
    if (-not $ok) { $failed = $true }
    Write-Host ("{0,-22} {1}" -f $suiteName, $(if ($ok) { "PASSED" } else { "FAILED (exit $($results[$suiteName]))" })) `
        -ForegroundColor $(if ($ok) { "Green" } else { "Red" })
}
if ($failed) { exit 1 }
exit 0
