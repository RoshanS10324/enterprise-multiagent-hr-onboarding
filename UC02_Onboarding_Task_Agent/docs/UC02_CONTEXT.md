# UC-02 — Onboarding Task Management: Verified Project Context

Detailed reference for AI/engineering sessions. Companion to [`../CLAUDE.md`](../CLAUDE.md)
(rules and safety live there; facts live here).

**Verified against the repository on 2026-10-16.** Branch `logesh`, commit `1c073df`,
working tree clean, 55 tracked files. Every number below was measured, not recalled.
Items that could not be verified are marked **UNVERIFIED**.

> **Staleness warning (2026-10-27).** Sections 2, 3, 5 and 8 describe commit `1c073df`
> and have **not** been re-measured since. Substantial uncommitted work has landed on
> the branch since then (now 484 tests, 16 source modules). Re-measure before quoting
> any line count, module list or test count from those sections; §10 and
> `docs/UC02_BUSINESS_RULES.md` are current.

---

## 1. Purpose and verified workflow

UC-02 plans and tracks the onboarding task checklist for a newly accepted hire. It is the
A10 agent under orchestrator O2, triggered conceptually by `offer.accepted`.

The workflow below is what the code actually does (read from `service.py`):

1. **Validate preconditions** — offer accepted (with timestamp) and onboarding form
   submitted, complete, and approved by an HR role. Otherwise `PreconditionError`.
2. **Generate the plan** — tasks come only from explicit per-client configuration
   (`ClientOnboardingConfig`), filtered by location / project / role, topologically
   ordered, with due dates computed in UAE business days from the joining date.
3. **Track execution** — `start_task` / `complete_task` enforce client isolation, owner
   authorization, prerequisites, and onboarding state. Independent tasks are
   simultaneously ready; dependents stay `BLOCKED`.
4. **Re-plan on change** — joining date / location / project changes go through a hard
   gate; on approval, affected tasks are rescheduled, newly applicable tasks added, and
   no-longer-applicable open tasks removed. Completed and cancelled tasks are never
   reopened, rescheduled, or removed.
5. **Surface risk** — overdue detection (derived from an explicit `as_of`, never stored)
   and day-1 joining risk.
6. **Prepare reminders** — bilingual EN/AR **drafts** only. Nothing is ever sent.
7. **Cancel** — gated; cancels all open tasks, preserves completed ones.

Cross-cutting: approval gates, append-only audit logging, per-client data isolation,
fully deterministic behaviour.

### Public API (`OnboardingService`, verified)

`create_onboarding` · `get` · `start_task` · `complete_task` · `ready_tasks` ·
`blocked_tasks` · `parallel_batches` · `request_change` · `decide_change` ·
`apply_change` · `request_cancellation` · `apply_cancellation` · `status` ·
`joining_risk` · `reminder_drafts`

Added since this section was measured (verify in `service.py` before relying on the
list): `milestones` · `outstanding_activities` · `owner_issues` · `calendar_status` ·
`escalations` · `joining_handoff` · `checklist` · `preview_checklist` ·
`configuration_issues`

Every method takes an `Actor` first and enforces client isolation before anything else.

---

## 2. Source map — `src/onboarding_task_management/` (2,743 lines)

| Module | Lines | Responsibility |
|---|---:|---|
| `service.py` | 656 | Orchestration; the only stateful component. Holds the client-partitioned record store. |
| `planner.py` | 313 | Plan generation, dependency resolution, parallel batches, re-planning. |
| `synthetic.py` | 293 | Synthetic clients, configs, personas. |
| `contracts.py` | 287 | Pydantic contracts, enums, derived views. |
| `checklist.py` | 217 | Client task-template config; validates keys, bilingual titles, deps, cycles. |
| `approvals.py` | 206 | Gate definitions, gate→operation binding, approver authorization. |
| `tracker.py` | 206 | Status transitions, authorization, overdue detection, joining risk. |
| `reminders.py` | 172 | Bilingual draft preparation. **No transport of any kind.** |
| `calendar_uae.py` | 147 | UAE business-day arithmetic + coverage-range validation. |
| `audit.py` | 92 | Append-only, client-scoped audit log. |
| `errors.py` | 82 | Typed errors; `code` values mirror the shared `ExceptionCode`. |
| `config.py` | 40 | Settings, thresholds, `AGENT_HOME`, `POLICY_PATH`. |
| `__init__.py` | 32 | Package docstring documenting the harness seams. |

### Legacy placeholders — do not delete without approval

`src/__init__.py`, `src/agent.py`, `src/config.py`, `src/contracts.py`,
`src/synthetic.py`, `src/testing.py` — all **0 bytes**, from the original scaffold.

> **Import-ambiguity risk (verified).** `src/` is on `sys.path`, so these shadow
> top-level names: `import config` resolves to the empty `src/config.py`, **not** the
> package module. Nothing breaks today because all imports are package-qualified.

---

## 3. Test map — `tests/` (3,017 lines, **257 tests**)

| File | Tests | Covers |
|---|---:|---|
| `test_security_regressions.py` | 39 | C1 gate binding, H1 mutable-record bypass, H2 audit isolation, M4, M5 |
| `test_policy_parity.py` | 29 | policy.yaml ↔ Python parity; enforcement classification |
| `test_hardening_regressions.py` | 28 | M3 Arabic titles/content, L3 calendar range, L4 cycles |
| `test_onboarding.py` | 26 | Happy path, preconditions, missing config, duplicates |
| `test_reminders.py` | 22 | Overdue, joining risk, drafts-never-sent |
| `test_authorization.py` | 19 | Unauthorized actions, client isolation, audit |
| `test_phase1_hardening.py` | 19 | N1 stale approval, N2 cancel replay, N3 unsafe re-plan, read auditing |
| `test_approvals.py` | 17 | Gate enforcement, rejection, approver authorization |
| `test_replanning.py` | 16 | Joining-date / location / project changes |
| `test_calendar_uae.py` | 14 | Business-day arithmetic |
| `test_cancellation.py` | 14 | Gated cancellation |
| `test_planner_dependencies.py` | 14 | Blocked prerequisites, parallel readiness |

Support: `tests/__init__.py` (puts `src/` on `sys.path`), `tests/support.py`
(`ServiceTestCase`; `record` is a live-read property because the service returns deep
copies), `tests/yaml_lite.py` (test-only YAML subset reader).

---

## 4. Repository structure — implemented vs. placeholder

Root mirrors the official Enterprise HR Agent layout (aligned in `1c073df`), but **most root
directories are empty**.

**Real content:** `src/` · `tests/` · `docs/` · `guardrails/policy.yaml` ·
`README.md` · `pyproject.toml` · `.gitignore` · `.dockerignore` · `CLAUDE.md`

**Empty `.gitkeep` placeholders (14, no code):** `brain/` · `cicd/` · `context_layer/` ·
`evals/` · `hooks/` · `knowledge/` · `mcp/` · `monitoring/` · `optimization/` ·
`policy/` · `skills/` · `specs/` · `tests/unit/` · `tests/integration/`

`data/` exists on disk, is empty and untracked.

> `tests/unit/` and `tests/integration/` are **empty**. The 257 tests are flat files
> directly in `tests/`. Both directories still contain stale, gitignored UC-03 `.pyc`
> bytecode from an earlier Roshan checkout — inert and uncommittable; removal is pending
> approval.

Deliberately **absent** (excluded during structural alignment as incompatible with UC-02):
`Dockerfile`, `uv.lock`, `pyrightconfig.json`, `.env.example`, `uc04-equipment-access/`.
Rationale is in the alignment report; summary: they assume `uv`, a FastAPI `src/main.py`,
Python 3.11, and Azure credentials, none of which apply here.

---

## 5. Verified environment and test results

| Item | Verified value |
|---|---|
| Python | 3.12.10 (`requires-python >=3.12`) |
| pydantic | 2.13.5 (`>=2.10.0` required) — the only runtime dependency |
| pytest | **9.1.1 — present** |
| pyyaml | **6.0.3 — present** |
| `uv` | **absent** (not on PATH) |

```
$ python -m unittest discover -s tests -t .
Ran 257 tests in 0.279s
OK

$ python -m pytest
257 passed, 86 subtests passed in 1.60s
```

> **Known doc drift:** `README.md` states that pytest and pyyaml are not installed. That
> was true when written; **both are now present**. `tests/yaml_lite.py` exists only
> because pyyaml was unavailable, so its rationale is now stale (see §8).

---

## 6. Architectural decisions

- **Harness reuse deferred, not rejected.** `docs/pre-development-review.md` §B
  recommends reusing `hr-recruitment-common` rather than duplicating ~1,400 lines. It is
  verified present in the sibling reference repo but **not installable** (`uv` absent),
  and that repo is a separate git repository so `{ workspace = true }` cannot span it.
  `pyproject.toml` declares the dependency **commented out** with the enabling condition.
  Decision **D-3** (monorepo merge vs. private index vs. path dep) is unresolved.
- **Harness seams.** `approvals.py` → `platform.hitl.HITLInbox`; `audit.py` →
  `platform.audit.AuditLog`; `service.py` → `agent.BaseAgent` + LangGraph. Service method
  names map 1:1 onto the planned graph nodes so the port is mechanical.
- **Determinism over convenience.** IDs derived (`ONB-<client>-<employee>`,
  `<onboarding_id>#<template_key>`, `-R<n>` for re-onboarding); all date logic takes an
  explicit `as_of`; overdue is derived, never stored.
- **Snapshots, not live records.** Reads return deep copies, so mutating a returned
  object cannot change stored state.
- **Gate binding.** An approval authorises only its own gate's operation, and only while
  the onboarding is `ACTIVE`.
- **Tests as `unittest`.** Chosen when pytest was unavailable; pytest collects them
  unchanged, so both runners work.

## 7. Constraints

- Synthetic data only. Contracts have **no** name/email/nationality/contact field; the
  audit log rejects personal-data keys.
- No network, no LLM, no provider SDK anywhere in `src/` or `tests/` (grep-verified).
- Reminders are drafts; there is no transport and `reminders.py` must stay that way.
- **UAE holiday calendar covers 2026-01-01 → 2027-12-31 only**, and those dates are
  synthetic/approximate placeholders — lunar holidays are **not** authoritative. Outside
  the window, operations raise `CalendarRangeError` rather than silently assuming no
  holidays. Legal/HR-approved data must be supplied before any date beyond 2027-12-31.
  Holidays must not be fabricated.

## 8. Known gaps

| Gap | Detail |
|---|---|
| No approved UC-02 spec | Requirements inferred from the blueprint; task content is placeholder pending HR Ops. Hard blocker B-1. |
| Policy largely unenforced | `tools`, `write_requires_approval`, `never_auto`, `autonomy`, `ceiling`, `approved_by` = `declared_not_enforced`; `volume_ceiling` = `unused_pending_decision` ("per run" undefined for onboarding). |
| `joining_confirmation` gate | Declared, `authorises: unused` — no operation consumes it. |
| Rehire after **completed** onboarding | Unsupported. `OnboardingStatus.COMPLETE` is never set by any code path, so only `CANCELLED` releases the duplicate check. Revisit when UC-06 closes records. |
| Read-audit retention | Successful reads are audited, but retention/volume limits are undefined. |
| `yaml_lite.py` now redundant | pyyaml is installed; the test-only parser could be replaced, and policy could be loaded by `src/` for real. Not done. |
| API / eval harness | Both built. The API ships at `src/onboarding_task_management/agent/sub_agent/api.py`; the scenario runner sits outside the package at `evaluation/run_evals.py`. They previously lived in a second `agent/` tree at the repo root. |
| Legacy placeholders | Six empty `src/*.py` files shadow top-level module names. |
| `approved_by: []` | Envelope not cleared for L1 live use. |
| Three-way policy mirror | YAML ↔ `approvals.GATES` ↔ `config`; parity tests make drift loud but don't remove it. |

## 9. Suggested next steps

Not started; priority order is a judgement call, not a commitment.

1. Resolve **D-3**, install `uv`, then integrate `hr-recruitment-common` and retire the
   three prototype seams.
2. Obtain the UC-02 specification and reconcile scope and task content.
3. Load `guardrails/policy.yaml` in `src/` via pyyaml, collapsing the three-way mirror.
4. Extend the holiday calendar with approved data.
5. Decide read-audit retention; decide `volume_ceiling`.
6. Delete the six legacy placeholders and the stale UC-03 `.pyc` files (both need approval).
7. Refresh `README.md`'s pytest/pyyaml statements.

---

## 10. Session handoff

Update this section after meaningful work; keep it short.

**Last verified:** 2026-10-27 · branch `logesh` · 484 tests passing (unittest and pytest,
571 subtests) · mypy clean (16 modules).

**Last session did:** Added explained dynamic checklist generation. New module
`src/onboarding_task_management/selection.py` (pure rules/explanation layer); new
contracts (`ChecklistContext`, `TemplateDecision`, `DimensionVerdict`, `ChecklistEntry`,
`ChecklistView`, `ChecklistPreview`, `ChecklistIssue`); `TaskTemplate.dimension_verdicts`
is now the single evaluation of the six applicability filters, with `applies_to` reduced
to its boolean form; `ClientOnboardingConfig.dependency_scope_conflicts()`; three service
methods (`checklist`, `preview_checklist`, `configuration_issues`); 68 new tests in
`tests/test_dynamic_checklist.py`; `demo/uc02_dynamic_checklist.py`. **No existing
selection, planning, tracking or re-planning behaviour was changed** — the 416 previously
passing tests still pass unmodified. Details in `docs/UC02_BUSINESS_RULES.md` §3.

**Open with the user (needs approval):**
- Commit/push of all uncommitted work on this branch.
- **D-10** (new): does declaring `family_insurance` imply `insurance_required`? Until HR
  Ops decides, the conflict is reported as `dependency_scope_conflict`.
- Deleting the six legacy `src/*.py` placeholders.
- Deleting stale UC-03 `.pyc` under `tests/unit/` and `tests/integration/`.
- Whether to add UC-02-adapted `Dockerfile` / `pyrightconfig.json` / `.env.example`.
