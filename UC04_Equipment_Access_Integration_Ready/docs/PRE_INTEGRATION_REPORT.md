# UC-04 pre-integration report

> **Layout note (2026-10-28).** This report is a dated record written before the project was restructured, so its paths are historical. Current equivalents:
> `uc04-equipment-access/` is now the repository root; `src/uc04/` is `src/sub_agent/`; `src/uc04_api/` is `src/sub_agent/api/`; `evals/` is `evaluation/`.
> The test counts (303, 442) and the commit commands in section 8 describe that earlier state. Do **not** run those `git add` commands. For the current
> layout, commands and verified results, use [README.md](../README.md).

Date: 2026-10-27. Branch `Roshan`, HEAD `ada8886` (pushed). Scope: mock data and mock adapters only. No database, Microsoft Entra, MCP, real IT service desk, parent orchestrator, LLM or network call was introduced. Nothing was committed or pushed.
**Target reached: pre-integration readiness using mock data. UC-04 is not production-ready.**

Evidence labels: **[V]** verified by running or reading it in this session; **[R]** previously reported, not re-run; **[A]** assumption.

## 1. Pre-integration audit

### 1.1 Baseline, before any change (2026-10-27 09:39) [V]
| Check | Result |
|---|---|
| Git | Branch `Roshan`, HEAD `ada8886` equal to `origin/Roshan`; 8 uncommitted items (README and pyproject modified; `docs/`, `run_api.py`, `src/uc04_api/`, 2 API test files untracked) plus a stray `grep.exe.stackdump` |
| Full suite | 303 passed (174 core + 129 API) |
| Evaluations | 12/12 |
| Demo | `run_demo.py` exit 0 |
| Swagger catalog | In sync with the registry; 62 scenarios |
| Static tools | ruff, mypy and pyflakes were not installed |

The earlier figures (303 tests, 12/12, 62/62 scenarios) were treated as historical and re-run, as instructed.

### 1.2 What the sources actually say about the three named gaps [V]
- **AC-07** ("Due dates respect lead times, items allowed after joining, and the working calendar"). Lead times and the calendar are fully specified by configuration. "Allowed after joining" appears only for documents: EC-08 (degree attestation, "if the client allows it after joining, proceed and track it") and a client example ("collect education certificates only after day one"). For IT, equipment and access the Framework says "must all be ready by day one". **No source defines a post-joining equipment or access item, so none was modelled.**
- **EC-35** ("Project or client changes before joining: new onboarding form version; cancel the old client-site requests and start the new ones"). Clearly specified, so it was implemented (section 3).
- **HC-08** appears once, in GR-20 ("approval at HC-08" before any spend), and is defined nowhere in either document. **Not implemented.** UC-04 raises no spend and only proposes workarounds; a test pins that down.

### 1.3 Defects found and fixed (each regression test was run and shown to fail before the fix) [V]
| Id | Finding | Evidence | Fix |
|---|---|---|---|
| D1 | An idempotent replay returned the old answer after the parent case became WITHDRAWN or ON_HOLD (for example `IN_PROGRESS` while the parent was withdrawn) | Probed before changing code | A stored result is replayed only while the case state and the parent status both match |
| D2 | A failed evaluation (privacy guard or any unexpected error) left half-applied state; a retry then lost the cancellation drafts | Code order: state is written before the final guard; reproduced by tests | The stored state is restored on failure and audited as `state.rolled_back` |
| D3 | EC-35: after a client change the old client-site requests stayed stored as orphans, with no cancellation, no draft and no report | Baseline experiment | Old client-site requests are cancelled and archived, draft cancellations are proposed for HR, and their request keys cannot be reused |
| D4 | The API accepted `"yes"` for `dev_reset_first`, and the orchestrator envelope accepted any `contract_version` (9.9, empty) | Probed | Strict boolean; only contract version 1.0 is accepted |

### 1.4 Probed, no defect found [V]
Malformed and unsupported API input (array body, wrong types, null input, over-long keys, bad `run_id`, empty roles) is refused with field names only. A failing mock desk never produces a false success, keeps state and blocks a blind retry (`ADAPTER_ERROR`, `PREVIOUS_ADAPTER_ERROR_UNRESOLVED`). Unknown case, client mismatch, idempotency conflict, kill switch, caller allowlist, privacy redaction and untrusted-text handling behave as specified. The AC-07 calendar and lead-time boundaries are correct (20 tests, no code change needed).

### 1.5 Limitations observed and deliberately not changed [V]
`as_of_date` is caller-supplied; the API has no payload size cap (1,500 items were accepted); `workflow.id` and `workflow.step` are not validated; `acknowledge_adapter_error` has no HTTP route; evidence records carry no timestamp (the last record listed wins); an item missing from a later payload stays stored unreported; the requester may approve their own request (the sources are silent). Each is recorded in `specs/UC04_SPEC.md` sections 12 and 13.

## 2. Requirement traceability update
| Requirement | Before | Now | Source | Implementation | Tests |
|---|---|---|---|---|---|
| EC-35 client change | Partial | **Implemented** | Framework section 11 | `service._cancel_old_client_site_requests`, `store.archived`, `ItemRecord.client_site` | `tests/test_client_change.py` (15) |
| AC-07 lead times and calendar | Partly applicable | **Verified, with boundaries** | Framework section 12 | `calendar.py`, `items._due`, `risk.py` (unchanged) | `tests/test_calendar_boundaries.py` (20) |
| AC-07 post-joining items | Not modelled | **Blocked: requirement unanswered for equipment and access** | Framework EC-08 (documents only) | none, by design | spec section 12 |
| HC-08 | Open question | **Open question, not implemented** | Framework GR-20 | none; spend is never raised | `tests/test_integration_boundaries.py` (9) |
| BR-21 idempotency, BR-22 cancellation, EC-20 | Implemented | **Strengthened (D1)** | assignment brief, Framework | `service.evaluate` replay check | `tests/test_failure_atomicity.py` (10) |
| BR-18 fail closed, BR-15 privacy | Implemented | **Strengthened (D2)** | assignment brief | `store.snapshot`, `store.restore` | `tests/test_failure_atomicity.py` |
| Input safety (API, envelope) | Implemented | **Strengthened (D4)** | n/a | `entrypoints/orchestrated.py`, `uc04_api/app.py` | `tests/test_api_hardening.py` (15) |

The full table is in `specs/TRACEABILITY.md` (section 8 and the updated rows). Counts after this review: business rules 24 of 24; decision rules 7 / 2 / 8; guardrails 18 / 2 / 1; edge cases now **23 implemented**, 0 partial, 29 outside UC-04 [V].

## 3. Code changes (UC-04 only)
| File | Change |
|---|---|
| `src/uc04/service.py` | Replay validity includes the parent status; snapshot and rollback around an evaluation; EC-35 client-change handling and reporting of archived items |
| `src/uc04/store.py` | `ItemRecord.client_site`, `CaseRecord.archived`, a four-part idempotency cache entry, `snapshot` and `restore` |
| `src/uc04/entrypoints/orchestrated.py` | Refuses an unsupported `contract_version` |
| `src/uc04/adapters/case_context_mock.py` | Mock-only `set_client` so the parent's client change can be simulated |
| `src/uc04_api/app.py`, `scenarios.py` | Strict boolean for `dev_reset_first`; two new Swagger scenarios (replay after WITHDRAWN, unsupported contract version) |
| `pyproject.toml`, `README.md`, `specs/`, `docs/` | Documentation and packaging, see section 8 |

Public contracts: unchanged except two tightenings. The envelope now refuses a `contract_version` other than "1.0", and `dev_reset_first` refuses non-boolean values. Both previously accepted inputs that no valid caller sends. All other input and output models, the policy file, the kill switch, the privacy guards and the audit fields are untouched. No dependency was added to the core (`pydantic` only).

## 4. Regression and boundary tests
| File | Tests | Covers |
|---|---|---|
| `tests/test_client_change.py` | 15 | EC-35: cancel and draft, archive, audit, new requests start fresh, stale approvals, key reuse refused, form not approved, missing configuration, repeated and replayed calls, failure rollback, determinism |
| `tests/test_failure_atomicity.py` | 10 | D1 replay vs parent status; D2 rollback after a privacy-guard or unexpected failure; audit of the rollback |
| `tests/test_calendar_boundaries.py` | 20 | AC-07 working-day arithmetic, holidays, zero and 365-day lead times, stalled and joining-date boundaries, no post-joining allowance |
| `tests/test_integration_boundaries.py` | 9 | Mocks match ports, business modules import no adapters, no credentials or URLs in source, one gated write tool, no spend, HC-08 recorded |
| `tests/test_api_hardening.py` | 15 | D4, a failing desk never looks like success, replay through the API follows the parent |
| `tests/test_api_scenarios.py` | 80 | Every one of the 64 Swagger scenarios, isolation, tags, catalog sync (was 78) |
| `tests/test_api.py` | 51 | API behaviour (unchanged this task) |
No existing test was weakened, skipped or edited in this task. The 11 original core test files are unchanged and still pass (174).

## 5. Validation report [V]
Run on 2026-10-27 in `uc04-equipment-access` (Windows, Python 3.14.4, pydantic 2.14.1, pytest 9.1.1, fastapi 0.159.1, httpx 0.28.1).
| Command | Result |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest -o addopts= -p no:cacheprovider -W ignore -q` | **374 passed** (174 original core + 54 pre-integration core + 146 API) |
| Same, original 11 core files only | 174 passed |
| `.\.venv\Scripts\python.exe evals\run_evals.py` | 12/12 match on both entry points |
| `.\.venv\Scripts\python.exe run_demo.py` | exit 0 |
| `python -m uc04_api.catalog` then the sync test | Catalog regenerated; in sync (64 scenarios) |
| `.\.venv\Scripts\python.exe run_api.py` (documented start command), then `/health`, `/docs`, `/openapi.json` | All HTTP 200; version 0.2.0; 8 tags; mode LOCAL_SIMULATION |
| All 64 Swagger scenarios sent over real HTTP to that server | **64 passed, 0 failed** |
| `dev_reset_first="yes"` and `contract_version="9.9"` over HTTP | Both 422, as intended |
| `ruff check --select F,E9` (installed in the local `.venv` only, not a project dependency) | `src/`, `evals/`, `run_*.py` and every new or changed test file: clean. 16 unused imports remain in the original, unmodified test files, left alone as out of scope |
| Byte-compile of `src`, `tests`, `evals` | OK |
| Type check | **Not run**: no type checker is configured for this package (the root mypy settings target the main app) |
| No-network check | API tests run in process; a fixture fails them on any non-loopback connection; none occurred |
Not re-run: the 8-case mutation spot-check from an earlier session **[R]**.

## 6. Open questions and blockers
Business owner (none is answered by the two source documents; see `specs/UC04_SPEC.md` section 12 and `docs/CONFIG_GAP_REPORT.md`):
1. HC-08: what it is, who approves at it, whether UC-04 ever raises spend.
2. AC-07: may any equipment or access item complete after joining, and how is that configured.
3. EC-35: how a project change (same client) is represented.
4. Real role and client matrices, approvers (and **who approves exceptions**), lead times, calendar source, evidence roles, IT ticket statuses, client-site profiles, country rules.
5. Separation of duties, evidence timestamps, how a single request is withdrawn, the real orchestrator envelope fields.
Dependencies outside UC-04: the database choice (Azure Cosmos DB or Supabase), the Microsoft Entra design and approval routing, the real IT service desk and its MCP endpoint, the parent orchestrator and its contract, repository CI for UC-04, the HR-system conflict (Zoho vs Workday or ADP, which affects the parent agent), and the agreed integration path (UC-03 and UC-04 share history; UC-02 on `logesh` and the empty `main` do not).

## 7. Pre-integration readiness checklist
| Item | Status |
|---|---|
| Business logic independent of external services | **Done** (tested) |
| Interfaces defined; mocks match them | **Done** (tested) |
| Applicable documented requirements implemented or recorded | **Done**; AC-07 post-joining and HC-08 **blocked** on business answers |
| Fail-closed behaviour and no false success on failure | **Done** (D1, D2, desk failure) |
| Kill switch, allowlist, L1 policy, privacy, idempotency, audit | **Done** (existing, re-verified) |
| Mock mode visible and no credentials in code | **Done** |
| API validation, status codes, startup, Swagger scenarios | **Done** (64/64 over HTTP) |
| Documentation matches the code | **Done** for the files listed in section 8 |
| Real configuration values | **Blocked** (business owner) |
| Authentication and verified approvals | **Not built** (out of scope by instruction) |
| Persistent storage and durable audit | **Not built** (database undecided) |
| Real IT desk via MCP, including a ticket-status read port | **Not built** |
| Parent orchestrator integration and the real envelope contract | **Not built** |
| Repository CI, tracing, monitoring, deployment | **Not built** |
| Type checking | **Not applicable** here (not configured) |

## 8. Changed files
Modified (tracked): `README.md`, `pyproject.toml`, `specs/TRACEABILITY.md`, `specs/UC04_SPEC.md`, `src/uc04/service.py`, `src/uc04/store.py`, `src/uc04/entrypoints/orchestrated.py`, `src/uc04/adapters/case_context_mock.py`.
Added (untracked): `run_api.py`; `src/uc04_api/` (5 files); `docs/` (`LOCAL_API.md`, `SWAGGER_TEST_CATALOG.md`, `CONFIG_GAP_REPORT.md`, `INTEGRATION_BOUNDARIES.md`, this report); `tests/test_api.py`, `test_api_scenarios.py`, `test_api_hardening.py`, `test_client_change.py`, `test_failure_atomicity.py`, `test_calendar_boundaries.py`, `test_integration_boundaries.py`.
Unmodified: all other `src/uc04/` modules, `data/`, `policy/`, `evals/`, `run_demo.py` and the 11 original test files.
Outside the repository: the reference notes `D:\dact-official\knowledge\05_team_update_10_points.md` and `06_uc04_one_pager.md` were corrected (kill-switch and branch-history statements).
Left for you to remove (a safety check blocked the deletion): `uc04-equipment-access/grep.exe.stackdump` (a Git Bash crash dump of 2026-10-22, harmless, not gitignored) and `C:\tmp\intro.txt` with its empty folder (a temp copy of extracted PDF text I created on 2026-10-22).
All of the above is uncommitted. Review, then commit with explicit paths (never `git add -A`):
```powershell
cd D:\dact-official\enterprise-multiagent-hr-onboarding
git status
git diff -- uc04-equipment-access/src uc04-equipment-access/specs
Remove-Item uc04-equipment-access\grep.exe.stackdump
# commit 1: business fixes and their tests
git add uc04-equipment-access/src/uc04 uc04-equipment-access/specs uc04-equipment-access/tests/test_client_change.py uc04-equipment-access/tests/test_failure_atomicity.py uc04-equipment-access/tests/test_calendar_boundaries.py uc04-equipment-access/tests/test_integration_boundaries.py
git commit -m "fix(uc04): EC-35 client change, replay vs parent status, rollback on failure"
# commit 2: local API, docs, packaging
git add uc04-equipment-access/src/uc04_api uc04-equipment-access/run_api.py uc04-equipment-access/tests/test_api.py uc04-equipment-access/tests/test_api_scenarios.py uc04-equipment-access/tests/test_api_hardening.py uc04-equipment-access/docs uc04-equipment-access/README.md uc04-equipment-access/pyproject.toml
git commit -m "feat(uc04): local Swagger API, pre-integration reports"
```
