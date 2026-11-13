# Client configuration: seeding guide (Supabase Development only)

**Status (2026-11-04): the dry run has been executed, read-only, against the Development project, for the agreed scope: the four `SYN-CLIENT-*` clients with UC-04's `role_access_matrix` EXCLUDED (`--no-uc04-matrix`). Seeding has NOT been run and is waiting for explicit approval.**

## Commands (PowerShell, from `UC02_Onboarding_Task_Agent`)

```powershell
python scripts\uc02_client_config.py validate     # offline: schema + semantic checks of the four profiles
python scripts\uc02_client_config.py dry-run --no-uc04-matrix      # READ-ONLY: selects only; exit 0 only if safe to apply
python scripts\uc02_client_config.py seed --no-uc04-matrix --confirm-write <target-host>   # WRITES, insert-only (needs the two settings below)
python scripts\uc02_client_config.py verify --no-uc04-matrix       # READ-ONLY read-back after seeding
```

`--no-uc04-matrix` leaves UC-04's `role_access_matrix` table untouched (its owners have not confirmed the projection); it is the agreed scope for now. Without the flag the plan is 244 rows (37 matrix rows more). `seed` is refused unless
`UC02_ENV=development`, `UC02_ALLOW_CONFIG_SEEDING=1`, the host is re-typed and the dry run is clean. It inserts only missing rows, parents first
(`client_configs`, `client_config_versions`, then templates, documents, policies, milestones, holidays, matrix), and never updates or deletes. A row that already
exists with different content is a **conflict**: it is reported, never overwritten, and blocks the run. No Production access exists in the tooling.

## Dry-run result (live Development, 2026-11-04)

Safe to apply: **yes**. Proposed inserts **207**, proposed updates **0**, deletes **0**, conflicts **0**, foreign-key problems **0**, document problems **0**.
The four client ids (`SYN-CLIENT-A`..`SYN-CLIENT-D`) did not exist, so there were no duplicates and nothing to no-op. Existing clients (`CLIENT_DUBAI`, `CLIENT_USA`,
`CLIENT_INDIA`, `CLIENT_CANADA`, `test_enterprise_corp`, `SYN-UAE-TEST`) and the synthetic workflow case `62e3b13e-...` are not touched.

| Table | Rows now | Proposed inserts | No-op | Conflicts | Expected after |
|---|---|---|---|---|---|
| `client_configs` | 6 | 4 | 0 | 0 | 10 |
| `client_config_versions` | 5 | 4 | 0 | 0 | 9 |
| `task_templates` | 18 | 57 | 0 | 0 | 75 |
| `client_document_requirements` | 6 | 55 | 0 | 0 | 61 |
| `client_service_policies` | 14 | 74 (50 assignment rules + 24 section rows) | 0 | 0 | 88 |
| `client_milestone_templates` | 0 | 4 | 0 | 0 | 4 |
| `client_holidays` | 4 | 9 | 0 | 0 | 13 |
| `role_access_matrix` | not in scope (`--no-uc04-matrix`) | 0 | - | - | unchanged |
| **Total** | | **207** | | | |

Configuration coverage per client (from the dry run):

| Client | Tasks | Assignment rules | Documents (mandatory) | Equipment / access items | Programmes | Policies | Holidays | Sections stored |
|---|---|---|---|---|---|---|---|---|
| SYN-CLIENT-A | 15 | 14 | 13 (11) | 3 / 3 | 3 | 4 | 2 | 6 |
| SYN-CLIENT-B | 11 | 9 | 12 (10) | 2 / 3 | 2 | 4 | 2 | 6 |
| SYN-CLIENT-C | 15 | 12 | 17 (16) | 3 / 3 | 3 | 5 | 3 | 6 |
| SYN-CLIENT-D | 16 | 15 | 13 (12) | 3 / 2 | 3 | 4 | 2 | 6 |

## What approving the dry run means

Approving authorises writing exactly those 207 rows to the Development project, nothing else, and nothing to `role_access_matrix`. The rows
carry `data_origin = synthetic` and the clients are named "SYNTHETIC CLIENT ... (not a real client)". The `approved` version status with `approved_by =
synthetic-fixture` is a development placeholder: synthetic configurations are used by a workflow only when the runtime opts in (`UC02_ALLOW_SYNTHETIC_CONFIG=1`).

## Rollback

Seeding is insert-only, so the rollback is deleting the same rows by natural key, children first: `role_access_matrix`, `client_holidays`, `client_milestone_templates`,
`client_service_policies`, `client_document_requirements`, `task_templates`, `client_config_versions`, `client_configs`. The exact list (207 entries with `--no-uc04-matrix`) is produced by
`seeding.rollback_plan(...)` and is reported by the dry run; it is **not executed by any command**. Run it only with explicit approval; because every key includes one of the four new client ids it
cannot touch any other client's rows.

## Idempotency and drift

Re-running `seed` inserts nothing (all rows already present) and reports the same counts. `verify` re-reads every projected row, compares content, checks that the six stored
sections rebuild the document and hash to `client_config_versions.config_hash`, and returns problems if anything was changed by hand. Tests for all of this run against the
mocked transport (`tests/test_client_configuration.py`); the live read-back runs only after seeding.

## After seeding (not yet done)

`verify` (live read-back) -> shared retrieval service and the four-client end-to-end tests -> documentation update with the actual results.
