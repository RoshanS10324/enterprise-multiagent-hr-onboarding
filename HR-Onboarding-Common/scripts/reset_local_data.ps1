<#
.SYNOPSIS
  Clear the employee / onboarding records the backend keeps LOCALLY, so it starts with no cases.

.DESCRIPTION
  Moves the local runtime data into a dated backup folder (nothing is deleted; delete the backup yourself when you no
  longer need it):
    HR-Onboarding-Common\.dact\orchestrator\     case states (the Onboarding Cases list), pre-intake cases, approvals,
                                                 checkpoints, UC03 document registry, audit log, UC05's local records
    HR-Onboarding-Common\.venv\Lib\local_db\     UC02's local task plans (UC02_STORE_MODE=sqlite)
  UC01 and UC04 keep their records in memory only: stopping the backend already clears them.

  Not touched: Supabase (nothing remote is read or changed), configuration (config\, client configurations), the code,
  and the ability to create new onboardings. Run it with the backend STOPPED: the script refuses otherwise.

.EXAMPLE
  .\scripts\reset_local_data.ps1
#>
$ErrorActionPreference = "Stop"
$common = Split-Path -Parent $PSScriptRoot

$running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "serve_orchestrator|uc_worker_runtime" }
if ($running) {
    Write-Host "The backend is still running (process ids: $(($running.ProcessId) -join ', '))." -ForegroundColor Red
    Write-Host "Stop it first (Ctrl+C in its window, or: Stop-Process -Id $(($running.ProcessId) -join ', ')), then run this again."
    exit 1
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path $common ".dact\backup-$stamp"
$targets = @(
    @{ Path = Join-Path $common ".dact\orchestrator"; Name = "orchestrator" },
    @{ Path = Join-Path $common ".venv\Lib\local_db"; Name = "uc02_local_db" }
)
$moved = @()
foreach ($t in $targets) {
    if (Test-Path $t.Path) {
        New-Item -ItemType Directory -Force -Path $backup | Out-Null
        Move-Item -Path $t.Path -Destination (Join-Path $backup $t.Name)
        $moved += $t.Path
    }
}
if ($moved) {
    Write-Host "Local onboarding data cleared. Moved to $backup :" -ForegroundColor Green
    $moved | ForEach-Object { Write-Host "  $_" }
    Write-Host "Start the backend again (e.g. .\scripts\start_local_uc01_memory.ps1): it starts with no cases."
} else {
    Write-Host "No local onboarding data found: nothing to clear."
}
