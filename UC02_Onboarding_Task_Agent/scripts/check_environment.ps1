# Read-only UC-02 configuration check. Prints which settings are present (never their values).
#
#   .\scripts\check_environment.ps1                 # everything the full workflow needs
#   .\scripts\check_environment.ps1 -Stage run      # only what one demo stage needs
#
# Stages: preflight, verify, setup, run, decide, send, documents, recover, full

param([string]$Stage = "full")

$ErrorActionPreference = "Stop"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "Python was not found on PATH. Install Python 3.12 or newer and try again."
    exit 1
}
& python (Join-Path $PSScriptRoot "uc02_env.py") check --stage $Stage
exit $LASTEXITCODE
