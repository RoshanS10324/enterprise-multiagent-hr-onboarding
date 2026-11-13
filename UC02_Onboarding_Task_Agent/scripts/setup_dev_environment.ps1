# First-time UC-02 development setup. Safe to run repeatedly. Never prints a secret and never writes to a database.
#
#   cd <repo>\UC02_Onboarding_Task_Agent
#   .\scripts\setup_dev_environment.ps1
#
# What it does: finds the UC-02 folder, loads .env then .env.local, writes UC02_STORE_MODE=supabase and UC02_ENV=development
# to .env.local if nothing defines them, generates any MISSING one of the five secrets into .env.local (git-ignored), then
# reports readiness. The Python entry points load .env.local themselves, so nothing has to be re-typed in a new PowerShell window.

$ErrorActionPreference = "Stop"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "Python was not found on PATH. Install Python 3.12 or newer and try again."
    exit 1
}
& python (Join-Path $PSScriptRoot "uc02_env.py") setup
exit $LASTEXITCODE
