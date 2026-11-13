<#
.SYNOPSIS
  Start the real onboarding backend with UC01 in memory and every downstream agent in its local mode, so UC01's
  output (case id, employee number, joining date) flows through UC02 -> UC03 -> UC04 || UC05 without Supabase writes.

.DESCRIPTION
  Use this while UC01 cannot write to Supabase (UC01_FIELD_ENCRYPTION_KEY is not set). UC01 keeps its case in memory,
  and the orchestrator hands UC01's result to the next agents through its local case store (SQLite), exactly as it
  does through case_orchestration_state in the Supabase setup.

  Where each part keeps its records in this mode:
    case state            SQLite  (<persist dir>\case_states.db)        survives a restart
    UC01                  memory                                        lost on restart; employee numbers restart at EMP-000001
    UC02 tasks / plans    SQLite  (UC02_Onboarding_Task_Agent\local_db)  survives a restart
    UC03 documents        memory + SQLite collection registry           files still go to the Storage bucket (no metadata row)
    UC04                  memory, configuration from config\uc04 files  lost on restart (the stage then reports UNCERTAIN)
    UC05                  local SQLite (<persist dir>\uc05)             survives a restart; buddies / welcome config read from Supabase
  Only tenants the local configuration knows can be planned: CLIENT-ALPHA (and the CLIENT-TEST-* demo tenants).
  CLIENT_DUBAI's onboarding configuration exists only in Supabase, so UC02 blocks with UC02_CONFIG_MISSING here.

  The overrides below apply to this PowerShell process only: the .env loader never replaces a variable that is
  already set, so HR-Onboarding-Common\.env is used for everything else and is not changed.

.EXAMPLE
  .\scripts\start_local_uc01_memory.ps1
  .\scripts\start_local_uc01_memory.ps1 -Port 8000 -PersistDir .dact\local
#>
param(
    [int]$Port = 8000,
    [string]$PersistDir = ""
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)   # HR-Onboarding-Common

$env:ORCHESTRATOR_STATE_STORE = "sqlite"
$env:UC02_WORKER_ENV__UC02_STORE_MODE = "sqlite"
$env:UC03_STORE_MODE = "memory"
$env:UC04_WORKER_ENV__UC04_STATE_MODE = "memory"
$env:UC04_CONFIG_SOURCE = "file"
$env:UC05_WORKER_ENV__UC05_DATABASE_MODE = "mock"   # UC05's local mode (an empty value would remove the variable)
$env:UC05_OFFER_EVENT_SOURCE = "orchestrator_case"  # UC05 reads the joiner from the orchestrator's case (UC01's result)
$env:UC05_BUDDY_SOURCE = "supabase"                 # read-only: buddy_profiles
$env:UC05_WELCOME_CONFIG_SOURCE = "supabase"        # read-only: client_configs.settings_payload.uc05_welcome

Write-Host "Local mode: UC01 in memory; case state, UC02 and UC05 in SQLite; UC03 and UC04 in memory." -ForegroundColor Cyan
$serveArgs = @("scripts\serve_orchestrator.py", "--dev-identity", "--port", "$Port")
if ($PersistDir) { $serveArgs += @("--persist-dir", $PersistDir) }
& .\.venv\Scripts\python.exe @serveArgs
