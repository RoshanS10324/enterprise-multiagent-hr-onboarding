# UC-02 — Architecture, Persistence and Agentic AI

> **Superseded in part (2026-11-04).** Statements here that Supabase persistence is unbuilt, unverified or blocked, that no LLM layer exists, or that only in-memory tests pass are historical. The verified state is in `UC02_FINAL_ARCHITECTURE_AND_STATUS.md`, `UC02_TRACEABILITY_MATRIX.md` and `UC02_INTEGRATION_CHECKLIST.md`.

Verified against branch `logesh` on 2026-10-27. **594 tests pass**, mypy clean (23 modules).
Every status claim below was produced by running the code, not by reading a docstring.

Companion documents: [`UC02_BUSINESS_RULES.md`](UC02_BUSINESS_RULES.md) (requirements →
implementation → tests), [`UC02_CONTEXT.md`](UC02_CONTEXT.md) (session context).

---

## 1. Status at a glance

| Component | Status | Evidence |
|---|---|---|
| Deterministic checklist + rules engine | **Implemented, tested** | `selection.py`, `planner.py`, `tracker.py`, `checklist.py` · 68 + 67 tests |
| Persistence layer and schema | **Implemented against a port; no database provisioned** | `persistence.py`, `supabase/migrations/*.sql` · 50 tests |
| **Supabase project** | **NOT CREATED** | No project, no credential, no SQL executed. §3 |
| Agent loop (retrieve→verify) | **Implemented, tested** | `agent/loop.py` · 60 tests |
| Tool layer + allowlist enforcement | **Implemented, tested** | `agent/tools.py` + `guardrails/policy.yaml` + harness `ToolGateway` |
| Shared harness (`hr-recruitment-common`) | **Installed and bound** | `agent/harness.py`; policy loads unchanged |
| LangGraph pipeline | **Implemented, tested** | `agent/harness.build_graph` |
| **LLM call** | **Path implemented; NEVER EXECUTED** | No credential configured. §5 |
| **MCP** | **NOT IMPLEMENTED** | `mcp/` holds only `.gitkeep`. §6 |
| Human approval + audit | **Implemented, tested** | `approvals.py`, `audit.py` |

---

## 2. Approved architecture this follows

Source: `../dact-core-functions-hr-sample-reference`, which is the Enterprise HR Agent reference
repository present on this machine.

* **ADR-005** — one repo per agent plus a shared harness; **LiteLLM is the only path to a
  model**; LangGraph for pipelines; autonomy enforced in `ToolGateway`, **not in prompts**.
  `langchain-anthropic`, the OpenAI SDK and `google-genai` are explicitly **not approved**,
  because they would be a second route to a model that bypasses the gateway.
* **ADR-003** — *models rate evidence, code computes bands*. UC-02 applies the same split:
  the model may explain and recommend; code decides.
* **ADR-001** — in-process orchestration now, Temporal before production. Persistence moves
  from SQLite/JSONL to **Postgres** (runbook item E6).

> **Supabase appears in no ADR.** Supabase *is* Postgres, so it is a plausible realisation of
> E6, but no decision record selects it. That remains open decision **D-9**, and it is why
> §3 stops short of provisioning anything.

### Dependency change made

`pyproject.toml` now declares `hr-recruitment-common` (path dependency) and
`litellm>=1.83.0`. The floor matters: **1.82.7 and 1.82.8 were malicious PyPI releases**
(ADR-005 §3). Installed here: litellm 1.106.4.

This resolves open decision **D-3** toward "reuse the harness". UC-02 nonetheless remains
importable **without** it — every harness import is inside a function, and
`HarnessOptionalityTests` runs the agent in a subprocess with `hr_recruitment_common`
blocked to prove it.

---

## 3. Supabase — what exists and what does not

### Not done, and why

**No Supabase project was created.** The CLI (v2.98.2) is authenticated on this machine and
four projects are visible, including one named "HR Agent". Creating a project, or applying
migrations into the existing shared one, is an outward-facing action against your
organisation's account, and `UC02_PROGRESS_REPORT.md` records that the shared schema needs
Tech Lead agreement — UC-04 has already diverged. So: schema and adapter delivered, nothing
provisioned.

**No SQL has been executed. No credential was fetched or stored.**

### What was delivered

| File | Contents |
|---|---|
| `supabase/migrations/20261027090000_uc02_init.sql` | Schema `uc02`: 8 tables, constraints, indexes, triggers |
| `supabase/migrations/20261027090100_uc02_rls.sql` | RLS enabled **and forced** on all 8 tables, tenant policies, grants |
| `.env.example` | Placeholders only. No real value, anywhere |

### Tables

`client_config_ref` · `onboarding` · `task` · `task_dependency` · `plan_revision` ·
`approval_request` · `agent_run` · `audit_event`

**Deliberately absent:**

* **No milestone table.** `MilestoneState` is derived from the tasks plus an explicit
  `as_of` (`milestones.py`). A table would be a second source of truth that goes stale the
  moment a task completes.
* **No overdue column.** Same reason (`tracker.is_overdue`).
* **No client configuration table.** `ClientOnboardingConfig` is code-owned and validated at
  load time. Only a content hash is stored, so a plan is traceable to the configuration that
  produced it without copying client content into a database.

### Rules the database enforces, not just the application

| Rule | Mechanism |
|---|---|
| One live onboarding per employee | partial unique index `where status <> 'cancelled'` |
| Evidence required before completion (UC02-BR-12) | `check (status <> 'complete' or not evidence_required or evidence_ref is not null)` |
| A requester cannot decide their own approval | `check (decided_by is null or decided_by <> requested_by)` |
| Audit is append-only | no UPDATE/DELETE grant, no UPDATE/DELETE policy, **and** a `before update or delete` trigger that raises |
| No personal data in audit detail | trigger mirroring `audit.FORBIDDEN_DETAIL_KEYS` |
| Tenant isolation | RLS `using`/`with check` on `client_id = uc02.current_client_id()` |

`uc02.current_client_id()` reads the JWT `client_id` claim, falling back to the
`uc02.client_id` session GUC. It returns NULL when neither is set, and `client_id = NULL`
never matches — **fail closed**.

`task_dependency` has no `client_id`, so its policy derives the tenant from both endpoint
tasks: an edge cannot be created across two clients.

### Security

* `anon` and `authenticated` get **no grants at all**. UC-02 writes are business operations
  with prerequisite, authorisation, evidence and approval rules that SQL cannot express; they
  go through the service, running server-side as `uc02_service`.
* The service-role key **bypasses RLS entirely** and must never reach a browser. `.env.example`
  says so at the point of use.
* `force row level security` is set so the table owner is also subject to the policies —
  without it the owner bypasses every policy and the isolation tests would be meaningless.

### To provision it (requires your authorisation)

```bash
# 1. Create a dedicated dev project, or choose an approved existing one.
supabase projects create dact-uc02-dev --org-id <ORG_ID> --region ap-northeast-1

# 2. Link this repository to it.
supabase link --project-ref <PROJECT_REF>

# 3. Review the two migrations, then apply them.
supabase db push

# 4. Fill .env from the project settings (never commit it).
cp .env.example .env

# 5. Enable the opt-in integration test.
UC02_RUN_DB_INTEGRATION_TESTS=1 python -m pytest tests/test_persistence.py
```

Step 5 currently **fails by design** with "a live database driver is not installed" rather
than passing vacuously: no Postgres driver is installed, so the last mile — a real
`TableClient` over PostgREST or psycopg — is the remaining work once a project exists.

### Seeding

`seed.py` runs selected `data/mock/` fixtures through the **ordinary**
`create_onboarding` path, so seeded data is indistinguishable from real use. It does not
upload JSON into tables. It is idempotent: ids are derived, and a fixture whose case already
exists is skipped, so progress made since the first seed is never reset
(`test_reseeding_does_not_reset_progress`).

The five seeded fixtures describe *valid* onboardings. The others describe rejected inputs
(offer not accepted, form unapproved, missing configuration) and stay as test inputs.

---

## 4. Persistence design

Three layers:

```
OnboardingService  ──►  OnboardingStore  ──►  TableClient (Protocol)
  business rules        model ↔ row            upsert / select / delete
                                                    │
                                     ┌──────────────┴──────────────┐
                              InMemoryTableClient          (to build) PostgREST
                              used by all 594 tests         or psycopg adapter
```

`InMemoryTableClient` is not a stub: it enforces primary keys, rejects personal-data keys in
audit detail, and refuses to mutate or delete an audit row — the same rules as the trigger.
That is what makes the persistence tests meaningful without a database.

**Failure handling.** `_persist` does not swallow exceptions. If the store rejects a write
during `create_onboarding`, the in-memory record is rolled back, the failure is audited as
`failed_persist`, and the error propagates. A write that did not happen is never reported as
success (`FailureHandlingTests`).

**Restart.** `OnboardingService.hydrate(client_id)` reloads cases and pending approvals.
`RestartTests` proves task progress, evidence references, plan revision, cancellation and
pending gates all survive, and that a restored inbox will not reissue an existing request id.

**Plan order is stored.** `task.plan_order` preserves the planner's topological order;
reading back sorted by `task_id` would present prerequisites after their dependants.

---

## 5. Agentic AI

### The loop

```
retrieve → analyse → advise → decide → execute → verify → persist
```

Fixed, not free-running. UC-02 touches work permits, employment paperwork and system access;
every step has to be explainable afterwards. The model contributes at `advise` only.

| Layer | File | Responsibility |
|---|---|---|
| Rules engine | `selection.py`, `planner.py`, `tracker.py` | Which tasks exist, dependencies, dates, risk. **Never a model.** |
| Tools | `agent/tools.py` | 9 allowlisted operations, each a thin wrapper over the service |
| LLM | `agent/advisor.py` | Explanation and one recommendation |
| Orchestration | `agent/loop.py`, `agent/harness.py` | The sequence; LangGraph under the harness |
| Approval | `approvals.py` + `RESTRICTED_ACTIONS` | Restricted actions become proposals |
| Audit | `audit.py` | Every step, ids and counts only |

### Tools

Read: `get_onboarding` · `get_checklist` · `inspect_blockers` · `preview_checklist` ·
`assess_risk` · `prepare_followup` · `verify_task_state`
Write (approval-gated): `request_approval` · `update_task_state`

All nine are on the `guardrails/policy.yaml` allowlist, and `tools`/`write_requires_approval`
have moved from `declared_not_enforced` to **`enforced`** — the harness `ToolGateway` raises
`PolicyViolation` for anything else, and refuses every write tool at autonomy L0
(`test_an_l0_agent_may_not_call_a_write_tool`).

### What stops the model steering the workflow

`validate_advice` checks every recommendation against the record before the agent acts. If
the model names an action that does not exist, points at a task from another onboarding,
recommends progressing a task whose prerequisites are unmet, or ignores an outstanding
configuration review, the advice is **overruled**: the deterministic recommendation is
substituted and the run is marked `degraded` with the reason. Six tests cover this, including
an adversarial stub.

### Verification

Every write is followed by a re-read. If the stored state disagrees with what was expected,
the run is reported as `EXCEPTION`, `actions_taken` stays empty, and
`agent.verification_failed` is audited — tested by injecting a verification that disagrees.

### LLM status — honest

**No model call has been made.** No `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` or `AZURE_API_KEY`
is set in this environment. `build_advisor()` therefore returns `OfflineAdvisor`, and every
run records `advisor=offline`, `model=None`.

The real path is written and reachable: `LiteLLMAdvisor` calls
`hr_recruitment_common.llm.LiteLLMGateway.structured(...)` with `AgentAdvice` as the response
schema. It is exercised in tests through stub advisors standing in for the gateway. To run it
live, set a route (`LLM_MODEL_WORKHORSE`) and a credential, and unset `UC02_LLM_OFFLINE`.

A model failure never fabricates an answer: `advise_with_fallback` catches it, degrades to the
deterministic advisor, and records the exception text.

---

## 6. MCP and integrations

**No MCP server exists.** `mcp/` contains only a `.gitkeep`. The `mcp` Python package happens
to be installed in this environment, but UC-02 imports nothing from it, and
`integration_status()["mcp_server"]` returns `False`, asserted by a test.

MCP also does not appear in any ADR for these agents: ADR-004 mentions a Brain API over MCP
as a *platform* service, which is not UC-02's to build.

Other integration seams (`providers.py`) remain **mock/local only**: no HRIS, ITSM, e-sign,
email or notification adapter exists. `hris.*` and `itsm.*` stay on the policy allowlist as
**declared-but-unimplemented** connectors, labelled as such in the YAML.

---

## 7. Shared Enterprise HR Agent architecture

| Concern | How UC-02 connects | Status |
|---|---|---|
| Policy envelope | `guardrails/policy.yaml` loads into the harness `PolicyStore` **unchanged** | Working |
| Tool contract | `ToolGateway` allowlist + autonomy + approval | Working |
| Agent pipeline | LangGraph `StateGraph` | Working |
| Audit / HITL | UC-02 has its own `audit.py`/`approvals.py`; harness equivalents exist | **Two implementations** — see below |
| Shared identifiers | `client_id`, `employee_id`, `onboarding_id` are opaque strings | No cross-UC registry exists |
| Upstream (UC-01) | `OnboardingTrigger` | Contract defined; no live producer |
| Downstream (UC-06) | `JoiningHandoff`; `case_closed_by_uc02` is always `False` | Contract defined; no consumer |
| ATS port | Not wired — UC-02 consumes an onboarding trigger, not a requisition | N/A by design |

> **Known duplication to resolve.** UC-02 has its own `AuditLog` and `ApprovalInbox`, and the
> harness has `platform.audit.AuditLog` and `platform.hitl.HITLInbox`. Both now exist in the
> same process. UC-02's versions are what the 594 tests exercise and what persists to
> `uc02.audit_event`. Converging them is real work and was not attempted here, because
> swapping the audit log would touch every test that asserts on audit behaviour.

---

## 8. Commands

```bash
python -m unittest discover -s tests -t .              # 594 tests
python -m pytest                                       # same, via pytest
MYPYPATH=src python -m mypy -p onboarding_task_management
python -m ruff check src tests demo

python demo/uc02_end_to_end.py                         # original workflow
python demo/uc02_dynamic_checklist.py                  # two contexts, two checklists
python demo/uc02_agentic_workflow.py                   # full agentic flow + restart
python demo/uc02_agentic_workflow.py EMP-001           # any synthetic persona
```

---

## 9. Remaining blockers

| # | Blocker | Owner |
|---|---|---|
| 1 | **No Supabase project.** Migrations unapplied; no SQL has run | Tech Lead — which project, and is the `uc02` schema acceptable in a shared database? |
| 2 | **No live `TableClient`.** No Postgres/PostgREST driver installed | Follows from 1 |
| 3 | **No LLM credential.** No model call has ever executed | Tech Lead / platform |
| 4 | **No MCP server**, and no ADR requiring one for UC-02 | Architecture |
| 5 | Duplicate audit/HITL implementations (UC-02's and the harness's) | UC-02, after 1–3 |
| 6 | No approved UC-02 specification; task content is placeholder | HR Ops (B-1) |
| 7 | UAE holiday data unapproved, coverage ends 2027-12-31 | Legal/HR (D-5) |
| 8 | **D-10**: does `family_insurance` imply `insurance_required`? | HR Ops |
| 9 | `volume_ceiling` "per run" undefined; read-audit retention undefined | HR Ops |
