# UC-02 — Onboarding Task Management: Progress Report

> **Superseded in part (2026-11-04).** Statements here that Supabase persistence is unbuilt, unverified or blocked, that no LLM layer exists, or that only 484 tests pass are historical. The verified state is in `UC02_FINAL_ARCHITECTURE_AND_STATUS.md`, `UC02_TRACEABILITY_MATRIX.md` and `UC02_INTEGRATION_CHECKLIST.md`.

**Prepared by:** Logesh · **Date:** 2026-10-21 · **Branch:** `logesh` · **Commit:** `1c073df`
**Audience:** Karthi (Technical Lead)

> **Historical snapshot — superseded.** This report describes the branch as it
> stood on 2026-10-21 (288 tests). It is kept as a dated record and is
> deliberately not rewritten. For current status see
> [`UC02_BUSINESS_RULES.md`](UC02_BUSINESS_RULES.md) (traceability, open
> decisions) and the repository `README.md`. As of 2026-10-27 the branch has
> **484 tests** and three modules that did not exist when this was written:
> `selection.py`, `providers.py` and `milestones.py`.

---

## 1. Objective

UC-02 plans and tracks the onboarding task checklist for a newly accepted hire: it validates
offer acceptance and onboarding-form approval, generates tasks from client-specific
configuration, and tracks owners, dependencies, due dates, milestones and delays through to
day-1 readiness.

---

## 2. Completed Work

All items below were verified by reading the source and running the test suite on 2026-10-21.

| Capability | Status | Evidence |
|---|---|---|
| Client-configured checklist generation | Implemented and verified | `checklist.py`, `planner.py` · 26 tests |
| Task creation, ownership, authorization | Implemented and verified | `tracker.py` · 19 tests |
| Dependencies, prerequisites, parallel readiness | Implemented and verified | `planner.py` · 14 tests |
| Due dates, UAE business calendar | Implemented and verified | `calendar_uae.py` · 14 tests |
| Re-planning (joining date / location / project) | Implemented and verified | `planner.replan` · 16 tests |
| Overdue detection, joining-date risk | Implemented and verified | `tracker.py` · 22 tests |
| Approval gates, audit logging, client isolation | Implemented and verified | `approvals.py`, `audit.py` · 39 + 19 tests |
| **Milestone tracking** | **Implemented and verified (added this audit cycle)** | `milestones.py` · 31 tests |
| Synthetic data and test coverage | Implemented and verified | `synthetic.py` · 288 tests total |
| Reminder **drafting** | Implemented and verified | `reminders.py` · bilingual EN/AR drafts |

Security hardening completed earlier in the cycle: approval-gate binding, prevention of
state mutation through returned records, client-scoped audit reads, and safe failure on
unsupported calendar dates — each covered by dedicated regression tests.

---

## 3. Current Status

**Code:** 14 source modules (~2,900 lines) in `src/onboarding_task_management/`. All business
logic is deterministic — no clock reads, no randomness, no network calls.

**Tests (live run, 2026-10-21):**
- `python -m unittest discover -s tests -t .` → **288 passed, 0 failed, 0 skipped**
- `python -m pytest` → **288 passed, 104 subtests passed**
- `python -m mypy src/onboarding_task_management` → **clean, 14 files**
- `ruff` reports 20 style-level findings; no ruff/mypy configuration exists in
  `pyproject.toml`, so these are tool defaults rather than agreed project checks.

**Database: not started.** This is the most important status item. A repository-wide and
all-branch search found **no Supabase schema, no migrations, no SQL files, no database
driver, no connection configuration and no environment-variable usage**. All UC-02 data is
held in in-memory Python structures (`service._records`, `audit._events`,
`approvals._requests`) and does not survive process exit. Data persistence has therefore
**not been tested**, because there is nothing to test against. Neither the shared Supabase
development database nor Azure Cosmos DB is connected in any form.

**Integration readiness:** No API or service interface exists (no FastAPI/Flask anywhere in
the codebase). The `evals/` directory contains only a placeholder — there is no evaluation
harness. There is no integration with the `hr-recruitment-common` shared harness.

**Reminder delivery is not implemented.** The system produces drafts only; every draft is
marked `delivery_status = "draft_not_sent"` and the module contains no transport code.
**Escalation logic does not exist** — a search returned no matches.

**Not production ready.** The work to date is a local, synthetic-data prototype.

---

## 4. Pending Work / Blockers

**Confirmed blockers**

1. **No UC-02 specification or Business Framework document exists** on any branch (all four
   remote branches searched). Current requirements are inferred from the blueprint, so task
   and milestone *content* remains placeholder pending HR Ops ownership.
2. **Shared Supabase schema has not been created.** UC-02 cannot integrate, and persistence
   cannot be tested, until the schema and table contracts are agreed and provisioned.
3. **Shared-harness decision (D-3) unresolved** — whether to merge into the monorepo, publish
   to a private index, or use a path dependency. UC-04 has already made a local copy of the
   shared contract while UC-02 deferred; the two use cases are diverging.
4. **`uv` is not installed**, so `hr-recruitment-common` cannot be installed or integrated.
5. **UAE public-holiday calendar covers 2026-01-01 to 2027-12-31 only**, with synthetic
   placeholder dates. Legal/HR-approved data is required before later dates can be handled.

**Needs investigation or a decision (not yet blocking)**

- Reminder delivery channel and escalation rules (owner, trigger thresholds, recipients).
- `volume_ceiling` — the "per run" unit is undefined for onboarding.
- Read-audit retention and volume limits.
- Rehire after a *completed* onboarding is unsupported; only cancelled onboardings can be
  re-run.
- Still unimplemented pending connectors/spec: ITSM ticket creation, buddy assignment,
  30/60/90 plan, visa/medical dependency gating, health-insurance enrolment.

---

## 5. Next Steps

1. **Obtain the UC-02 specification** from HR Ops / the product owner, and confirm real
   checklist and milestone content. *(Dependency: HR Ops — highest priority.)*
2. **Agree and provision the shared Supabase schema**, then implement and test a persistence
   layer for onboarding records, approvals and audit events. *(Dependency: Tech Lead.)*
3. **Resolve D-3 and install `uv`**, then integrate `hr-recruitment-common` and retire the
   three local prototype seams. *(Dependency: Tech Lead / AI Architect.)*
4. **Define reminder delivery and escalation rules**, and confirm the notification connector,
   before any message-sending capability is built.
5. **Add the service/API layer and evaluation harness** once the harness decision and schema
   are settled.

---

*All figures in this report were produced by a live inspection and test run of branch
`logesh` at commit `1c073df` on 2026-10-21. No production system was accessed and no
credentials were read or exposed.*
