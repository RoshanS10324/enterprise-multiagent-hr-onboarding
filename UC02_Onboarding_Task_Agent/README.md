# UC-02 — Onboarding Task Management

Onboarding task management agent for the A10 onboarding agent (orchestrator O2, trigger `offer.accepted`).

> **Status (2026-11-04): UC-02-owned development is complete and tested on a Supabase-only runtime with synthetic data, a Fake LLM and mock Outlook / UC-03 / UC-04 / UC-05 counterparts.
> Verified on the Supabase Development project. Not production ready.** Pending and external: real email delivery (Microsoft Graph configuration), real approved client
> configuration, the HR approval portal, real UC-04/UC-05/UC-03 endpoints, a scheduler and delivery for reminders. See
> [`docs/UC02_FINAL_ARCHITECTURE_AND_STATUS.md`](docs/UC02_FINAL_ARCHITECTURE_AND_STATUS.md),
> [`docs/UC02_TRACEABILITY_MATRIX.md`](docs/UC02_TRACEABILITY_MATRIX.md) and
> [`docs/UC02_INTEGRATION_CHECKLIST.md`](docs/UC02_INTEGRATION_CHECKLIST.md). Sections below that describe a "Phase 1 prototype" are historical where they disagree.

Quick start (PowerShell, from this folder): `.\scripts\setup_dev_environment.ps1` once, then
`python demo\uc02_supabase_agentic_workflow.py preflight` and the stages listed in the integration checklist.

---

## Purpose and scope

UC-02 plans and tracks the onboarding task checklist for a newly accepted hire:
it validates that the offer was accepted and the onboarding form approved,
generates tasks from explicit per-client configuration, tracks owners, statuses,
due dates and dependencies, re-plans when the joining date / location / project
changes, flags overdue work and joining-date risk, and prepares reminder drafts.

### In scope (implemented)

- Offer-acceptance and onboarding-form approval validation
- Deterministic task generation from explicit client configuration
- Task IDs, owners, statuses, due dates, dependencies
- Parallel-ready tasks and blocking on unmet prerequisites
- Re-planning on joining-date / location / project change
- Overdue detection and joining-date risk assessment
- Reminder **drafts** — never sent
- Approval gates, audit logging, per-client data isolation

### Explicitly out of scope for Phase 1

- Sending any message (UC-06 / notification connector)
- HRIS, ITSM, e-sign and email connectors
- LLM-generated content (e.g. a 30/60/90 plan)
- `hr-recruitment-common` shared-harness integration

---

## Modules

All shipped source lives in `src/onboarding_task_management/`. The scenario
runner sits outside it, at `evaluation/`, because it is a verification
harness rather than library code: `python evaluation/run_evals.py`.

| Module | Responsibility |
|---|---|
| `contracts.py` | Pydantic contracts, enums, derived views. No name/email/contact fields exist. |
| `config.py`, `errors.py` | Settings/thresholds and typed errors. |
| `calendar_uae.py` | UAE business-day arithmetic with explicit coverage-range validation. |
| `audit.py` | Append-only, client-scoped audit log. |
| `persistence.py` | Durable store behind a `TableClient` port, with a faithful in-memory implementation. No database driver. |
| `providers.py` | Integration-boundary Protocols with **mock/local adapters only**. No transport of any kind. |
| `synthetic.py` | Synthetic clients, configs and personas. |
| `seed.py` | Idempotent seeding of synthetic fixtures through the normal service path. |

**`sub_agent/` — the working process.** Everything the use case actually does:

| Module | Responsibility |
|---|---|
| `sub_agent/checklist.py` | Client task-template configuration; validates keys, bilingual titles, dependencies, cycles. |
| `sub_agent/selection.py` | Explained checklist selection: which templates apply, why, and what needs human review. |
| `sub_agent/planner.py` | Deterministic plan generation, dependency resolution, parallel batches, re-planning. |
| `sub_agent/milestones.py` | Derived milestone state (never stored), computed from the plan and an explicit `as_of`. |
| `sub_agent/tracker.py` | Status transitions, authorization, overdue detection, joining-date risk. |
| `sub_agent/approvals.py` | Approval gates, gate-to-operation binding, approver authorization. |
| `sub_agent/reminders.py` | Bilingual reminder draft preparation. **No transport of any kind.** |
| `sub_agent/connectors/outlook/` | The delivery port and its **mock** adapter. Simulated delivery still requires an approval id. |
| `sub_agent/service.py` | Orchestration; every public method takes an `Actor` and enforces isolation. |
| `sub_agent/adapter.py` | Binds a request, the advisor, the service and the mock connector into one run. |
| `sub_agent/api.py` | FastAPI surface over that adapter. Optional extra: `pip install -e '.[api]'`. |
| `sub_agent/fakes.py` | Constructors over the advisor and the mock connector. Re-exports, never a second copy. |

**`agent/` — the agentic layer.** Drives the working process through an allowlist:

| Module | Responsibility |
|---|---|
| `agent/tools.py` | The nine allowlisted operations an agent may perform. |
| `agent/advisor.py` | LLM seam: deterministic offline advisor, plus the LiteLLM gateway path. |
| `agent/loop.py` | The fixed agent sequence: retrieve → analyse → advise → decide → execute → verify. |
| `agent/harness.py` | Optional binding to the shared Enterprise HR Agent harness: policy, `ToolGateway`, LangGraph. |
| `agent/fake_llm.py` | The one deterministic fake LLM. No network, no key, no provider SDK. |

### Key behaviours

- **Deterministic.** IDs are derived (`ONB-<client>-<employee>`,
  `<onboarding_id>#<template_key>`); all date logic takes an explicit `as_of`.
  No clock reads in business logic, no randomness, no network.
- **Gate-bound approvals.** An approval is valid only for the operation its
  gate authorises, and only while the onboarding is active.
- **Snapshots, not live records.** Reads return deep copies, so mutating a
  returned object cannot change stored state.
- **Client isolation.** Records and audit events are partitioned by `client_id`
  and scoped from the acting `Actor`, never from a caller-supplied id.

---

## Synthetic data only

No real employee data is present or permitted. Personas are opaque IDs plus a
scenario label; the contracts have no name, email, nationality or contact
fields at all, and the audit log actively rejects personal-data keys.

---

## Setup and dependency limitations

Python **3.12** and **pydantic ≥ 2.10** (2.13.5 present) are the only runtime
requirements. `pydantic` is the single third-party import anywhere in `src/`.

| Tool | Version present | Notes |
|---|---|---|
| Python | 3.12.10 | `requires-python >=3.12` |
| pydantic | 2.13.5 | only runtime dependency |
| pytest | 9.1.1 | optional; the suite is stdlib `unittest` and pytest collects it unchanged |
| mypy | 2.4.0 | no rule configuration pinned |
| ruff | 0.17.1 | no rule configuration pinned |
| pyyaml | 6.0.3 | installed, but **not used by `src/`** — see below |
| `uv` | **absent** | `hr-recruitment-common` cannot be installed; the shared harness is unavailable |

> **Known residue.** `tests/yaml_lite.py` is a test-only YAML subset reader
> written when `pyyaml` was unavailable. `pyyaml` is now present, so the reader
> is redundant and `guardrails/policy.yaml` could be loaded by `src/` for real.
> Neither change has been made; `yaml_lite.py` is still the only reader used by
> `tests/test_policy_parity.py`.

`pyproject.toml` declares the `hr-recruitment-common` dependency **commented
out**, with the condition for enabling it. This is deliberate: the reference
repo is a separate git repository, so `{ workspace = true }` cannot span it,
and decision **D-3** (merge into monorepo vs. private package index vs.
cross-filesystem path) is unresolved.

---

## Running the tests

```
python -m unittest discover -s tests -t .
```

Expected:

```
Ran 594 tests in ~10s

OK (skipped=1)
```

The one skip is the opt-in live-database test, which stays skipped until a
Supabase project exists and `UC02_RUN_DB_INTEGRATION_TESTS=1` is set.

Also verified:

```
python -m pytest                                      # 593 passed, 1 skipped
MYPYPATH=src python -m mypy -p onboarding_task_management   # 16 source files, clean
python -m ruff check src tests demo                   # see note below
```

`ruff` runs with its defaults (no `[tool.ruff]` section is pinned), so it
reports pre-existing stylistic findings — import ordering, `dict()` literals,
quoted forward references. None are errors and none are new.

No network access, no credentials and no services are required. The suite is
deterministic and should produce identical results on repeated runs.

---

## Running the demo

Two demonstrations of the complete workflow exist. Run them from this folder in PowerShell. Neither sends a real email or provisions a real
account; the Fake LLM, Outlook, document validation, access provisioning and UC-04/UC-05 are labelled mocks.

### A. Isolated local mock demo (no Supabase, no network, no files, nothing real)

```powershell
python demo\uc02_local_mock_demo.py              # compact report; exit code 0 only if all 22 checks pass
python demo\uc02_local_mock_demo.py --verbose    # also dumps every stage's full result
```

Needs only Python and the project dependencies - no `.env`, no Supabase, no secrets (it builds its own in-memory environment, ignores any real
`SUPABASE_*` / `UC02_*` variables, blocks network access and generates throw-away secrets in memory). It runs the real UC-02 code over an
in-process stand-in for the Supabase REST API that enforces the live schema, and each run starts from an empty database, so it is repeatable.
Stages: fixture -> UC-01 event -> configuration -> dynamic checklist -> Fake LLM review -> HR email draft -> signed HR approval -> authorised sends
(mock Outlook) -> mock document verification and mandatory gate -> mock UC-04/UC-05 triggers -> parallel task waves with mock access provisioning ->
due dates, overdue and monitoring sweep -> remaining waves -> restart recovery and duplicate-event replay -> read-back.

### B. Supabase Development integration demo (writes UC-02 records for the synthetic case)

```powershell
.\scripts\setup_dev_environment.ps1                 # once: UC02_STORE_MODE / UC02_ENV and local secrets into .env.local; no database access
.\scripts\check_environment.ps1 -Stage full
python demo\uc02_supabase_agentic_workflow.py preflight                       # read-only
python demo\uc02_supabase_agentic_workflow.py run --mock-downstream           # writes
python demo\uc02_supabase_agentic_workflow.py decide --decision approved --mock-downstream
python demo\uc02_supabase_agentic_workflow.py send --mock-downstream
python demo\uc02_supabase_agentic_workflow.py documents --mock-downstream
python demo\uc02_supabase_agentic_workflow.py tasks --mock-downstream --waves 2
python demo\uc02_supabase_agentic_workflow.py tasks --mock-downstream --waves 0 --as-of 2026-11-20   # overdue + sweep only
python demo\uc02_supabase_agentic_workflow.py recover --mock-downstream
python demo\uc02_supabase_agentic_workflow.py verify                          # read-only read-back
```

This uses the shared Development database and the existing synthetic case. The `tasks` stage is safe to repeat: finished tasks are never touched again and
each sweep item is recorded once. It does **not** complete the whole plan unless asked (`--waves N`): in the last Development verification 4 of 13 tasks were complete,
7 were ready (waiting for their owner or external use case to act) and 2 (`medical_test`, `biometrics`) were blocked behind `visa_status_change`. That is the true
state, not a defect.

**Reminders:** overdue status is derived from due dates and the clock; the monitoring sweep records escalation records and reminder **drafts**. There is **no
scheduler** and nothing is sent automatically; a scheduler that calls the sweep and a delivery channel (UC-06 / the real Outlook connector) are future integration work.

### Older read-only demos

`demo/uc02_dynamic_checklist.py` (two employees, two checklists, with reasons), `demo/uc02_cross_use_case.py`, `demo/uc02_business_workflow.py` and
`demo/uc02_persistent_workflow.py` are earlier local demonstrations of parts of the flow (SQLite / in-process; no Supabase). `demo/uc02_end_to_end.py` and
`demo/uc02_agentic_workflow.py` are **retired** (they ran on memory) and print where to go instead.

### Mock fixtures

`data/UC02_Synthetic_Onboarding_Test_Data.json` is the canonical fixture and is
never modified. Additional scenario fixtures live in `data/mock/` — see
[`data/mock/README.md`](data/mock/README.md) for the scenario index. All are
fictional and labelled with a `_mock_note` field.

### Integration boundaries

`src/onboarding_task_management/providers.py` defines the seams a future
production adapter will implement — employee data, client configuration,
calendar, repository, notification and the UC-06 handoff. **Only mock/local
adapters exist.** There is no HTTP client, database driver, credential handling
or external SDK anywhere in the package.

---

## Known limitations

### Calendar coverage ends 2027-12-31

The UAE public-holiday list covers **2026-01-01 to 2027-12-31** only, and the
dates in it are **synthetic/approximate placeholders** — lunar holidays are not
authoritative.

Dates outside the window raise `CalendarRangeError` rather than being silently
treated as holiday-free. Request-time validation means an unsupported joining
date is refused before an approval is raised, and re-planning fails without
mutating the record.

> **Action required:** legal/HR-approved UAE holiday data must be supplied and
> the coverage window extended **before any date beyond 2027-12-31 can be
> handled**. Holidays must not be fabricated.

### Re-onboarding

A **cancelled** onboarding can be followed by a new one; the new record gets an
`-R<n>` suffix, and the cancelled record and its audit history are preserved and
never overwritten.

**Rehire after a *completed* onboarding is not supported.** `OnboardingStatus.COMPLETE`
is never set by any current code path, so only `CANCELLED` releases the
duplicate check. This must be revisited when UC-06 begins closing records.

### Legacy placeholder files

Six empty files remain from the original scaffold and are **retained
deliberately**, pending approval to remove:

```
src/__init__.py  src/agent.py  src/config.py
src/contracts.py  src/synthetic.py  src/testing.py
```

> **Import-ambiguity risk.** Because `src/` is placed on `sys.path`, these
> shadow top-level module names: `import config` resolves to the empty
> `src/config.py`, **not** `onboarding_task_management/config.py`. Nothing
> breaks today because all imports are absolute and package-qualified, but the
> trap is live for new code. Prefer
> `from onboarding_task_management import config`. These files should be
> deleted once approved.

### Empty scaffold directories

Fourteen directories exist but contain **no code** — only a `.gitkeep`. They
mirror the official Enterprise HR Agent repository layout (aligned in commit `1c073df`) and
are retained for that reason:

```
brain/  cicd/  context_layer/  evals/  hooks/  knowledge/  mcp/
monitoring/  optimization/  policy/  skills/  specs/
tests/unit/  tests/integration/
```

In particular `mcp/` contains **no MCP server**, `evals/` contains **no
evaluation harness**, and `tests/unit/` and `tests/integration/` are empty —
all tests are flat files directly under `tests/`. Do not infer capability
from these directory names.

### Persistence, agent and LLM status

Full detail is in [`docs/UC02_ARCHITECTURE.md`](docs/UC02_ARCHITECTURE.md). In short:

| Thing | Status |
|---|---|
| Schema + RLS migrations | Proposal only in `supabase/migrations/`. **Not applied**; UC-02 uses the existing shared tables (no migration needed) |
| **Supabase project** | Shared Development project; schema verified; UC-02 persistence live-verified with the synthetic case |
| Persistence code + tests | `SupabaseTableClient` over REST (live-verified); `InMemoryTableClient`/SQLite for unit tests only; restart recovery proven live |
| Live `TableClient` | Built (`supabase_store.py`, `supabase_rest.py`); no Postgres driver needed |
| Agent loop, tools, verification | Working and tested |
| Shared harness + LangGraph | Installed and bound; `policy.yaml` loads unchanged |
| **LLM call** | Fake LLM (synthetic, labelled) is used; **no real model call has ever been made** |
| **MCP** | **Not implemented.** `mcp/` is an empty placeholder |

The agent runs with `advisor=offline` and `model=None` until a LiteLLM route and a
credential are configured. ADR-005 makes `hr_recruitment_common.llm.LiteLLMGateway` the only
permitted route to a model; direct provider SDKs are not approved.

### Policy enforcement

`guardrails/policy.yaml` is the declared envelope but is **not yet loaded by
code** (no `pyyaml`). Each key carries an explicit enforcement status —
`enforced`, `declared_not_enforced`, or `unused_pending_decision` — with a
documented reason, and `tests/test_policy_parity.py` asserts the YAML and the
Python mirrors agree.

`tools` and `write_requires_approval` are now **enforced** by the harness `ToolGateway`.
Still not enforced: `never_auto`, `autonomy`, `ceiling`, `approved_by`. Blocked on a business decision: `volume_ceiling`
(the "per run" unit is undefined for onboarding).

### Other open items

- No approved UC-02 specification exists; requirements are inferred. Task
  content is placeholder pending HR Ops ownership.
- Successful reads are now audited, but **read-audit retention and volume
  limits are undefined** and need a decision.
- `approved_by` is empty — the envelope is not cleared for L1 live use.
- Audit log and approval inbox are in-memory prototype seams, intended for
  replacement by `hr_recruitment_common.platform.{audit,hitl}`.

---

## Repository state

Work is on branch `logesh`. The Supabase-only runtime, environment tooling, task lifecycle stage and the final documents are committed
(`5b2a9f5`); the isolated local demo was added after that commit. Quality limitation: `python -m mypy src` reports 39 findings in 10 files (mostly
`calendar_uae.py` and older `**kwargs` typing); the code is not mypy-clean repo-wide.

### Documentation index

| Document | Contents |
|---|---|
| `README.md` | This file: scope, modules, setup, limitations. |
| `docs/UC02_FINAL_ARCHITECTURE_AND_STATUS.md` | **Authoritative.** Verified architecture, workflow, persistence mapping, results. |
| `docs/UC02_TRACEABILITY_MATRIX.md` | **Authoritative.** Requirement -> code -> tables -> tests -> status. |
| `docs/UC02_INTEGRATION_CHECKLIST.md` | **Authoritative.** Contracts, email configuration list, external dependencies, run commands. |
| `docs/UC02_ARCHITECTURE.md` | Earlier design (persistence section superseded by the final architecture document). Persistence, Supabase schema, agent, LLM/MCP status, remaining blockers. |
| `docs/UC02_BUSINESS_RULES.md` | **Current.** Business-rule → implementation → test traceability, issue codes, agentic-readiness table, open decisions D-1…D-10. |
| `docs/UC02_CONTEXT.md` | Session context and handoff. §§2, 3, 5, 8 are measured against commit `1c073df` and are flagged stale. |
| `docs/UC02_PROGRESS_REPORT.md` | Dated status report (2026-10-21) to the Technical Lead. Historical snapshot. |
| `docs/uc01-uc02-uc03-workflow.md` | Cross-use-case workflow narrative, with `.mmd` Mermaid sources beside it. |
| `docs/pre-development-review.md` | Pre-implementation analysis and the original open decisions. Historical. |
| `data/mock/README.md` | Index of the scenario fixtures. |
