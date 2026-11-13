# Pre-Development Review: UC-02 — Onboarding Task Management
## Revision 2 — Extended Analysis

**Prepared by:** Logesh (Senior Python Engineer / AI Agent Architect / QA Engineer)
**Revision 1 date:** 2026-10-15  ·  **Revision 2 date:** 2026-10-15
**Branch:** `logesh`
**Target repository:** `F:\enterprise-multiagent-hr-onboarding`
**Reference repository:** `F:\dact-core-functions-hr-sample-reference`
**Status:** Review only — no implementation started. No application code written, modified or deleted.

> **What changed in Rev 2:** Added §A (business-framework gap analysis), §B (shared-package analysis with concrete recommendation), §C (importability and dependency verification), §D (explicit UC-01→UC-06 data contracts), updated §7.2 checklist with classification, and added §E (build/test boundary matrix). All other sections are preserved from Rev 1 and lightly annotated where new evidence changes the reading.

---

## 0. Documents Inspected

### Rev 1 (already read)

| File | Source | Status |
|---|---|---|
| `docs/hr-agentic-ai-blueprint.md` (917 lines) | Reference repo | ✅ |
| `docs/wave-1-runbook.md` (310 lines) | Reference repo | ✅ |
| `docs/wave-2-plan.md` (163 lines) | Reference repo | ✅ |
| `docs/adr/ADR-001` through `ADR-005` | Reference repo | ✅ |
| `CLAUDE.md` | Reference repo | ✅ |
| `README.md` | Reference repo | ✅ |
| `hr-recruitment-common/src/hr_recruitment_common/agent.py` | Reference repo | ✅ |
| `hr-recruitment-common/src/hr_recruitment_common/contracts.py` | Reference repo | ✅ |
| `hr-recruitment-common/src/hr_recruitment_common/config.py` | Reference repo | ✅ |
| `hr-recruitment-common/src/hr_recruitment_common/synthetic.py` | Reference repo | ✅ |
| `hr-recruitment-common/src/hr_recruitment_common/testing.py` | Reference repo | ✅ |
| `hr-recruitment-common/src/hr_recruitment_common/platform/{hitl,policy,audit,tools,guardrails}.py` | Reference repo | ✅ |
| `hr-recruitment-candidate-matching-agent/src/…/{agent,api}.py` | Reference repo | ✅ |
| `hr-recruitment-candidate-matching-agent/guardrails/policy.yaml` | Reference repo | ✅ |
| `hr-recruitment-candidate-matching-agent/evaluation/run_evals.py` | Reference repo | ✅ |
| `hr-recruitment-candidate-matching-agent/tests/test_candidate_matching.py` | Reference repo | ✅ |
| `src/{agent,contracts,config,synthetic,testing}.py`, `tests/test_onboarding.py`, `pyproject.toml` | Target repo | ⚠️ All empty (1 byte) |

### Rev 2 (newly read)

| File | Source | Findings |
|---|---|---|
| `hr-recruitment-common/src/hr_recruitment_common/llm/{__init__,base,gateway,callbacks}.py` | Reference repo | ✅ Full LLM abstraction stack confirmed |
| `hr-recruitment-common/src/hr_recruitment_common/context_layer/{__init__,ontology,knowledge,memory}.py` | Reference repo | ✅ `Brain` dataclass, `Ontology`, `KnowledgeIndex`, `EpisodicMemory` confirmed |
| `hr-recruitment-common/src/hr_recruitment_common/platform/skills.py` | Reference repo | ✅ `SkillLibrary` (SKILL.md front-matter loader) confirmed |
| `hr-recruitment-common/src/hr_recruitment_common/api.py` | Reference repo | ✅ `create_app()` factory fully read; API surface documented |
| `hr-recruitment-common/src/hr_recruitment_common/connectors/ats/base.py` | Reference repo | ✅ `ATSPort` Protocol and data models confirmed |
| `hr-recruitment-common/pyproject.toml` | Reference repo | ✅ Package name, version, 13 runtime dependencies pinned |
| `hr-recruitment-candidate-matching-agent/pyproject.toml` | Reference repo | ✅ Agent only lists `hr-recruitment-common` as a dep; `[tool.uv.sources]` workspace: true |
| `hr-recruitment-candidate-matching-agent/requirements.txt` (311 lines) | Reference repo | ✅ Full locked dependency tree (uv-exported) |
| `python --version` (system Python) | Local machine | ✅ Python 3.12.10 installed |
| `pip show hr-recruitment-common` | Local machine | ❌ **Not installed** — `ModuleNotFoundError` |
| `pip show langgraph litellm` | Local machine | ❌ **Not installed** |
| `pip show pydantic fastapi` | Local machine | ✅ pydantic 2.13.5, fastapi 0.141.1 installed (system site-packages) |
| `uv --version` | Local machine | ❌ **`uv` is not installed** — not on PATH |
| UC-01 through UC-06 specification document | Both repos | ❌ **Not found** (confirmed after full search) |

---

## 1. Business Requirements Summary
*(Rev 1 content preserved; annotations added below)*

### 1.1 Context

The system operates under UAE jurisdiction (Federal Decree-Law 45/2021 PDPL, UAE Labour Law 33/2021). All agents are capped at autonomy **L1 (Draft for Approval)** at launch, with a target of **L2** for checklist and ticket-generation tasks after evidence accumulates (≥3 months, ≥90% approval rate, <10% material edits). Language: Arabic + English (bilingual outputs, RTL support). Python 3.12.

### 1.2 A10 — Pre-boarding & Onboarding (the parent agent)

| Attribute | Value |
|---|---|
| Blueprint ID | A10 |
| Agent ID (proposed) | `onboarding-task-management` *(to confirm with framework team — see C-5)* |
| Orchestrator | O2 (Onboarding & Employee Lifecycle) |
| Trigger event | `offer.accepted` |
| Start autonomy | L1 |
| Target autonomy | L2 (checklists, ITSM tickets) |
| Hard gates | Start-date changes; role changes; sending joining instructions |
| Eval bar | All day-1 items done on time ≥95% |
| Data tier | T2 (personal data — names, role, joining date) |
| Wave | Wave 2 |

### 1.3 Six use cases (inferred — spec document not found)

| UC | Inferred name | Blueprint anchor |
|---|---|---|
| UC-01 | Employee Record Creation | A10 trigger from `offer.accepted`; HRIS record |
| **UC-02** | **Onboarding Task Management** *(assigned)* | A10 checklist, task plan, delay alerts, date-change gate |
| UC-03 | Document & Information Validation | A09 pre-employment verification; document-collection tasks |
| UC-04 | Equipment & Access Management | ITSM ticket creation; provisioning tasks |
| UC-05 | Orientation & Policy Acknowledgement | 30/60/90 plan draft; handbook; buddy assignment |
| UC-06 | Joining Confirmation & Follow-up | Joining instructions; `employee.started` event |

> ⚠️ **Unconfirmed.** These names and boundaries are inferred from the blueprint and wave-2-plan. The spec document must be obtained before implementation.

---

## Section A — Business-Framework Gap Analysis

### A.1 What the framework says UC-02 (A10) must do

Drawing exclusively from confirmed source documents:

| Requirement (source) | Status in target repo |
|---|---|
| **Triggered by `offer.accepted`** (wave-2-plan §3.2) | ❌ Not implemented — `agent.py` empty |
| **Generate onboarding checklist per function/location** (wave-2-plan K2-4) | ❌ Checklist content not supplied (see C-6) |
| **Create IT/Facilities ITSM tickets** (wave-2-plan §3.2, P5) | ❌ No ITSM connector |
| **Assign buddy** (blueprint A10 description) | ❌ No buddy-pool data |
| **Draft 30/60/90 plan** (blueprint A10 description) | ❌ Requires LLM call; `FakeLLM` handler needed for tests |
| **Track task status and detect delays** (blueprint A10 eval bar: ≥95% on-time) | ❌ No tracking logic |
| **Handle joining-date changes via hard gate** (wave-2-plan §3.2) | ❌ No gate defined |
| **Visa/medical status dependency check** (wave-2-plan: "Day-1 dependencies on visa/medical status") | ❌ A11 status flags not available — cross-agent data contract undefined |
| **Mandatory health insurance enrolment** (wave-2-plan §3.2) | ❌ No insurance connector |
| **Bilingual task titles and instructions** (UAE requirement, CLAUDE.md rule 3, blueprint D3) | ❌ Not started |
| **L1 → L2 autonomy path for checklists and tickets** (wave-2-plan §3.2) | ❌ policy.yaml missing |
| **Audit trail per PDPL/UAE Labour Law** (blueprint §7.7) | ❌ Not implemented |

### A.2 Missing requirements / conflicts identified

| # | Issue | Source conflict | Risk |
|---|---|---|---|
| MR-1 | **No UC specification document exists.** All requirements above are derived from A10 description in wave-2-plan and blueprint. The real UC boundaries (where UC-02 ends and UC-03/UC-04 begin) are unconfirmed. | Gap | High |
| MR-2 | **A11 (Work Authorisation) publishes "status flags only" to other agents** (wave-2-plan §3.2: "status flags only to other agents (T4)"). UC-02 must check visa status before completing provisioning tasks, but A11 is a different agent and the cross-agent data contract is undefined. | Blueprint §3.2 vs. UC-02 scope | High |
| MR-3 | **Health insurance enrolment** is listed as a day-1 dependency (wave-2-plan §3.2) but no connector or data model is specified in either repo. It is unclear whether UC-02 creates an enrolment task or delegates to a benefits agent. | Blueprint gap | Medium |
| MR-4 | **Buddy / mentor assignment** is listed in the blueprint as an A10 task but no buddy pool data source is identified in either repo or the wave-2-plan knowledge list. | Blueprint gap | Medium |
| MR-5 | **30/60/90 plan** is listed as an A10 output but wave-2-plan does not specify whether it is LLM-generated (requiring `rc.llm()`) or template-filled (deterministic). The choice materially affects test design and policy. | Ambiguous | Medium |
| MR-6 | **`Scope.requisition_ids`** is the existing scope field in `AgentRequest`. For onboarding, the natural scope unit is `employee_id` / `onboarding_id`, not a requisition. This means either the `Scope` model must be extended or the agent must use `instance` as the onboarding identifier. | `contracts.py` (Scope model) | Medium |
| MR-7 | **`AgentContext.ats`** is typed as `ATSPort` (required). For an onboarding agent that never touches the ATS, passing a mock ATS is awkward. The context builder (`AgentContext.build`) always requires an `ats` argument. An alternative context or a stub ATS must be accepted. | `agent.py` (AgentContext) | Low-Medium |
| MR-8 | **`EpisodicMemory._scrub`** removes keys named `name`, `email`, `nationality`, etc. from any dict written to memory. If task titles or employee data happen to use these key names, they will be silently dropped. Field naming in UC-02 contracts must avoid these protected keys. | `memory.py` (PROTECTED_KEYS) | Low |

### A.3 Requirements that CAN be confirmed from existing documents

| Requirement | Confirmed from |
|---|---|
| Python 3.12, `uv` workspace, `hatchling` build backend | `hr-recruitment-common/pyproject.toml`, CLAUDE.md |
| `langgraph ≥ 0.4.0`, `langchain-core ≥ 0.3.0` | `hr-recruitment-common/pyproject.toml` |
| `pydantic ≥ 2.10.0` (Pydantic v2 only) | `hr-recruitment-common/pyproject.toml` |
| `litellm ≥ 1.83.0` (floor explicitly stated; 1.82.7/1.82.8 are blocked) | `hr-recruitment-common/pyproject.toml`, ADR-005 |
| `fastapi ≥ 0.115`, `uvicorn[standard] ≥ 0.30` | `hr-recruitment-common/pyproject.toml` |
| `pyyaml ≥ 6.0`, `tenacity ≥ 9.0`, `python-dotenv ≥ 1.0`, `httpx ≥ 0.28` | `hr-recruitment-common/pyproject.toml` |
| `opentelemetry-api/sdk ≥ 1.20.0`, `tzdata ≥ 2024.1` | `hr-recruitment-common/pyproject.toml` |
| `pytest` (dev dependency) with `--import-mode=importlib` | `hr-recruitment-sample-reference/pyproject.toml` |
| `jurisdiction = "AE"`, `default_timezone = "Asia/Dubai"` | `config.py` (hardcoded) |
| Audit log: JSONL in dev, append-only | `platform/audit.py` |
| HITL inbox: SQLite in dev | `platform/hitl.py` |
| `EpisodicMemory`: SQLite, thread-safe `RLock` | `context_layer/memory.py` |
| Agent must never call LLM SDK directly | CLAUDE.md rule 1 |
| No real PII in any fixture | CLAUDE.md rule 2 |
| Gates, autonomy, allowlists only in `policy.yaml` | CLAUDE.md rule 3 |
| Numbers from code, never from model | CLAUDE.md rule 4 |

---

## Section B — Shared Package: Reuse vs. Separate Harness

### B.1 Option analysis

#### Option A — Import `hr-recruitment-common` directly as a `uv` workspace member

**How the reference does it:**
```toml
# hr-recruitment-candidate-matching-agent/pyproject.toml
[project]
dependencies = ["hr-recruitment-common"]

[tool.uv.sources]
hr-recruitment-common = { workspace = true }
```
The monorepo root `pyproject.toml` declares both `hr-recruitment-common` and the agent folder as workspace members. `uv sync` installs the common package in editable mode from the local path.

**Advantages:**
- **Zero duplication.** All 18 shared modules (`agent.py`, `contracts.py`, `hitl.py`, `policy.py`, `audit.py`, `tools.py`, `guardrails.py`, `llm/`, `context_layer/`, `api.py`, `observability/`, `synthetic.py`, `testing.py`) are reused unchanged.
- **Compatibility guaranteed.** Same `uv.lock` means common and agent packages share the exact same resolved dependency tree.
- **Parity with other Enterprise HR Agent agents.** Every other agent in the ecosystem follows this pattern (ADR-005 §1). Diverging creates a third packaging scheme.
- **No maintenance burden.** Security fixes, guardrail improvements, and contract changes in `hr-recruitment-common` flow to the onboarding agent automatically on `uv sync`.
- **`AgentContext`, `BaseAgent`, `HITLInbox`, `AuditLog`, `ToolGateway`, `FakeLLM`** are immediately available and tested.
- **`create_app()`** gives the FastAPI layer for free.

**Risks and mitigations:**

| Risk | Mitigation |
|---|---|
| `ATSPort` in `AgentContext.build()` requires a concrete ATS mock even though onboarding doesn't use the ATS | Build a `StubATS` (3 lines) that satisfies the `ATSPort` Protocol with `raise NotImplementedError`; or extend `AgentContext.build()` with an `ats=None` default (proposed in §D) |
| `Scope.requisition_ids` does not map naturally to `onboarding_id` | Use `instance` field of `WorkflowRef` as the onboarding identifier; extend `Scope` or accept misuse of the existing field |
| `Brain.load()` points to `context_layer/data/ontology/technology/` — technology role ontology is irrelevant for onboarding | Provide an onboarding-specific `context_dir` or use only `KnowledgeIndex` (knowledge search) and `EpisodicMemory`; skip `Ontology` |
| The target repo is a **separate Git repository** from the reference | The two repos cannot share a `uv` workspace directly (workspace = files in the same directory tree). Onboarding must either: (a) be merged into the reference monorepo as a sub-folder, or (b) install `hr-recruitment-common` from a published PyPI/private package index, or (c) install it via a path dependency pointing across the filesystem |

**Path option within a separate repo:**
```toml
# pyproject.toml (target repo)
[tool.uv.sources]
hr-recruitment-common = { path = "F:/dact-core-functions-hr-sample-reference/hr-recruitment-common", editable = true }
```
This works locally but breaks in CI and containers unless the path is parameterised. It is an interim workaround, not a production pattern.

---

#### Option B — Create a separate `hr-onboarding-common` package

**Advantages:**
- Full control; can tailor `AgentContext` to drop the ATS dependency and add HRIS/ITSM ports.
- No coupling to the recruitment repo release cycle.

**Risks:**
- **High duplication.** Must copy or rewrite: `contracts.py` (156 lines), `agent.py` (198 lines), `hitl.py` (169 lines), `policy.py` (101 lines), `audit.py` (46 lines), `tools.py` (48 lines), `guardrails.py` (201 lines), `llm/` (4 modules), `observability/` (2 modules), `api.py` (146 lines), `config.py` (80 lines), `synthetic.py`, `testing.py` — **~1 400 lines** before writing a single line of onboarding logic.
- **Contract drift.** If `AgentRequest`, `Proposal`, or `Autonomy` change in the reference repo, the onboarding harness diverges silently.
- **Violates ADR-005 §1** ("one shared package").
- **CLAUDE.md rule 3** says gates and autonomy levels live in `policy.yaml`, which is enforced by `PolicyStore` — duplicating `PolicyStore` doubles the risk of enforcement gaps.

---

### B.2 Recommendation

> **Recommendation: Option A — import `hr-recruitment-common` as a local path (editable) dependency while the repository structure is confirmed.**

**Rationale from evidence:**
1. The reference `pyproject.toml` declares `hr-recruitment-common` as `version = "0.1.0"` with `hatchling` as the build backend. It is installable via `pip install -e .` from its directory.
2. The candidate-matching agent's `pyproject.toml` has exactly one line: `dependencies = ["hr-recruitment-common"]` with `[tool.uv.sources] hr-recruitment-common = { workspace = true }`. The onboarding agent can mirror this exactly, substituting `{ path = "..." }` until the repos are restructured.
3. The `AgentContext`, `BaseAgent`, and `contracts.py` contracts are stable (version `1.0` contract, no breaking changes observed across all modules). The only adaptation needed is a `StubATS` for the `ats` parameter.
4. The `EpisodicMemory._scrub` protected-key list does not conflict with the proposed UC-02 field names (task_id, onboarding_id, role_id, category are all safe).
5. Duplicating ~1 400 lines of shared infrastructure is an unacceptable risk given the Enterprise HR Agent guardrail rules that must be **identical** across agents.

**Prerequisite for Option A:** The AI Architect / repo owner must decide whether:
- (a) The onboarding repo is **merged** into the reference monorepo (recommended long-term), or
- (b) `hr-recruitment-common` is **published to a private package index** (PyPI / Artifactory / GitHub Packages) that the onboarding repo can `pip install` from.

Until that decision is made, use the local path reference in `[tool.uv.sources]` for development. **Do not copy-paste the source files.**

---

## Section C — Importability and Dependency Verification

### C.1 System Python environment (verified by running commands)

| Check | Result |
|---|---|
| `python --version` | **Python 3.12.10** ✅ (matches `.python-version` = `3.12`) |
| `uv --version` | ❌ **`uv` is NOT installed** on this machine. Not found on PATH. |
| `pip show hr-recruitment-common` | ❌ **`ModuleNotFoundError`** — package not installed in any sys.path location |
| `pip show langgraph` | ❌ Not installed |
| `pip show litellm` | ❌ Not installed |
| `pip show pydantic` | ✅ pydantic **2.13.5** (satisfies `>=2.10.0`) |
| `pip show fastapi` | ✅ fastapi **0.141.1** (satisfies `>=0.115`) |
| `pip show opentelemetry-api` | ❌ Not installed |
| `pip show python-dotenv` | ❌ Not installed |
| `pip show pyyaml` | ❌ Not installed |

**Summary:** The target machine has **Python 3.12.10** and a partial system install of `pydantic` and `fastapi`. It **does not have `uv`** (the primary build tool), and the `hr-recruitment-common` package is **not importable**. No tests can be run without first installing `uv` and running `uv sync`.

### C.2 What `uv sync` would install (from reference `requirements.txt`)

The candidate-matching agent's `requirements.txt` (311 lines, autogenerated by uv) locks these key transitive deps:

| Package | Locked version | Compatible with onboarding? |
|---|---|---|
| `litellm` | `1.104.2` | ✅ (above `>=1.83.0` floor; avoids malicious 1.82.7/1.82.8) |
| `langgraph` | `1.2.14` | ✅ |
| `langchain-core` | `1.6.9` | ✅ |
| `pydantic` | `2.14.0` | ✅ (system has 2.13.5; either satisfies `>=2.10.0`) |
| `fastapi` | `0.143.0` | ✅ (system has 0.141.1; minor version difference, non-breaking) |
| `opentelemetry-api/sdk` | `1.45.1` | ✅ |
| `openai` | `2.54.0` | ⚠️ Installed transitively by litellm; **must not be imported in agent code** (ADR-005 §3) |
| `boto3` | `1.43.110` | ⚠️ Transitively required by litellm; not used by agents |
| `colorama` | `0.4.6` (Windows only) | ✅ |
| `uvloop` | Not installed on Windows (`sys_platform != 'win32'`) | ✅ (conditional marker) |

### C.3 Contract compatibility between `hr-recruitment-common` and the onboarding agent

All shared contracts from `contracts.py` are compatible with the proposed UC-02 design **with two adaptations**:

| Contract | Compatibility | Required adaptation |
|---|---|---|
| `AgentRequest[InputT]` | ✅ Generic over any Pydantic model | `InputT = OnboardingTaskInput` |
| `AgentResponse` | ✅ Proposals, exceptions, feedback, provenance | No change |
| `Proposal` | ✅ `payload: dict[Any]` is open | No change |
| `Autonomy` (L0/L1/L2/L3) | ✅ | Policy ceiling = L2 |
| `ExceptionCode` | ✅ All existing codes apply | `MISSING_DATA` for missing employee, `POLICY_BREACH` for gate violations |
| `Actor` + `Scope` | ⚠️ `Scope.requisition_ids` semantics don't fit onboarding | Use `WorkflowRef.instance` = `onboarding_id`; treat `Scope.requisition_ids` as `[onboarding_id]` with a comment |
| `AgentContext.build(ats=...)` | ⚠️ `ats` is required | Pass `StubATS()` — a 3-line class implementing `ATSPort` Protocol with no-op methods |
| `Brain.load(context_dir)` | ⚠️ Points to `technology` ontology | Use `KnowledgeIndex` and `EpisodicMemory` only; create a minimal onboarding ontology dir or override `context_dir` |
| `HITLInbox`, `AuditLog`, `ToolGateway` | ✅ No changes needed | Used as-is |
| `FakeLLM` | ✅ Register handlers via `.on(task, handler)` | Register a handler for `"draft_30_60_90_plan"` and `"bilingual_task_titles"` |
| `create_app()` | ✅ `agent_cls.input_model` is resolved at runtime | Works with `OnboardingTaskAgent` |

### C.4 The `AGENT_HOME` resolution pattern

From `config.py` (`resolve_agent_home`):
```python
# agent.py top-level
AGENT_HOME = resolve_agent_home(__file__)
# → if AGENT_HOME env var is set: uses it
# → otherwise: Path(__file__).resolve().parents[2]
# → for src/<package>/agent.py, parents[2] = the agent folder root
```
This means the agent finds `guardrails/policy.yaml` at `<AGENT_HOME>/guardrails/policy.yaml` automatically, provided the folder structure follows:
```
<agent-root>/
├── guardrails/policy.yaml
├── context_layer/skills/
└── src/<package>/agent.py    ← parents[2] = <agent-root>
```
The target repo currently has `src/agent.py` (not `src/<package>/agent.py`), so `parents[2]` would resolve to the **parent directory of the repo**, not the repo root. The `src/` layout must be changed to `src/onboarding_task_management/agent.py` before `AGENT_HOME` auto-resolution works correctly.

---

## 2. Six-Use-Case Dependency Matrix
*(Rev 1 content preserved)*

```
Trigger: offer.accepted (from O1 / ATS)
    │
    ▼
UC-01 Employee Record Creation
 ─ Creates the canonical HRIS employee record
 ─ Emits: employee_id, joining_date, role, department, manager_id
 ─ Dependency for: ALL subsequent UCs
    │
    ▼
UC-03 Document & Information Validation
 ─ Collects: passport copy, degree attestation, certificates, medical
 ─ Checks: required documents per role / nationality (deterministic)
 ─ Status flag published: document_status ∈ {pending | complete | overdue}
 ─ Gate: HR Ops approves before UC-04 can provision access
 ─ Blocks: UC-04 (no access provisioning without document clearance for visa roles)
    │
    ▼
UC-04 Equipment & Approved Access Management
 ─ ITSM tickets: laptop spec, system access, building access, email setup
 ─ Reads: role profile → standard IT kit list (deterministic)
 ─ Gate: IT approves provisioning; HR Ops approves non-standard items
 ─ Prerequisite: UC-03 clearance for visa-dependent access
 ─ Parallel with: UC-05 (orientation can start while kit is provisioned)
    │
    ├──────────────────────────────────────────────┐
    ▼                                              ▼
UC-02 Onboarding Task Management              UC-05 Orientation & Policy Acknowledgement
 ─ Orchestrates the full task checklist        ─ Schedules Day-1 orientation session
 ─ Assigns owners and due dates                ─ Sends bilingual handbook + policies
 ─ Tracks status per task                      ─ Records acknowledgement (e-sign or click)
 ─ Detects delays; fires alerts                ─ Assigns buddy / mentor
 ─ Handles joining-date changes                ─ Generates 30/60/90 plan draft (L1 proposal)
 ─ Gate: HR Ops for date changes               ─ Gate: HR Lead approves 30/60/90 plan
 ─ Cross-dependency: reads UC-04               ─ Prerequisite: UC-01 record exists
   task status to determine overall            ─ Parallel with: UC-04
   onboarding progress
    │                                              │
    └─────────────────────┬────────────────────────┘
                          ▼
                  UC-06 Joining Confirmation & Follow-up
                   ─ Confirms day-1 readiness (all tasks green)
                   ─ Sends bilingual joining instructions to employee
                   ─ Fires escalation if any task is still open T-2 days before joining
                   ─ Records completion; emits employee.started event
                   ─ Gate: HR Ops must confirm before instructions are sent (L1)
                   ─ Brain feedback: onboarding_outcome signal → memory
```

---

## Section D — Explicit UC-01→UC-06 Data Contracts

> **Legend:** `[PROPOSED]` = designed for this review, not yet confirmed by product team. `[FROM BLUEPRINT]` = directly stated in a source document. `[FROM REFERENCE CODE]` = observed in the reference repo implementation.

### D.1 UC-01 → UC-02 (trigger contract)

**Direction:** UC-01 completes → UC-02 starts

```python
# [PROPOSED] The O2 orchestrator passes this to UC-02 via AgentRequest.input
class OnboardingTrigger(BaseModel):
    """The data UC-01 must produce for UC-02 to begin. Sent by the O2 orchestrator."""
    employee_id:   str          # [PROPOSED] Canonical HRIS ID; no name or contact
    joining_date:  date         # [FROM BLUEPRINT] Required for due-date calculations
    role_id:       str          # [FROM BLUEPRINT] Maps to onboarding checklist type
    level:         str          # [PROPOSED] L1–L5; affects kit list and plan depth
    department:    str          # [PROPOSED] e.g. "technology"
    manager_id:    str          # [PROPOSED] For buddy assignment and plan review gate
    location:      str          # [PROPOSED] "Dubai, UAE" or free-zone; affects visa tasks
    nationality_flag: str | None = None  # [FROM BLUEPRINT] "uae_national" | "non_national" | None
                                         # Emiratisation flag only — not used in scoring
    emiratisation_applicable: bool = False  # [FROM BLUEPRINT A10/A11]
```

**Unconfirmed:** Does UC-01 send this as a single `AgentRequest` to UC-02, or does the O2 orchestrator parse an ATS webhook and construct it? This affects whether UC-02 handles its own triggering or is purely reactive.

### D.2 UC-02 → UC-03 (document task hand-off)

**Direction:** UC-02 creates a `document_collection` task → UC-03 is the executor

```python
# [PROPOSED] UC-02 writes this task record; UC-03 updates its status
class DocumentTask(TaskRecord):
    category = TaskCategory.DOCUMENT_COLLECTION
    # UC-03 reads from this and writes back document_status
    required_documents: list[str]   # [PROPOSED] e.g. ["passport", "degree_cert", "medical"]
    submitted_documents: list[str]  # [PROPOSED] filled by UC-03
    document_status: str = "pending"  # [PROPOSED] pending | complete | overdue | exception
```

**Status flag interface (UC-03 → UC-02):**
```python
# [PROPOSED] UC-03 publishes this; UC-02 reads it to unblock UC-04
class DocumentStatusFlag(BaseModel):
    onboarding_id:   str
    employee_id:     str     # ID only — no PII
    document_status: str     # "pending" | "complete" | "overdue"
    missing_docs:    list[str]
    as_of:           datetime
```

**Note:** UC-03 must NOT publish individual document contents — only status flags (T2 access boundary).

### D.3 UC-02 → UC-04 (provisioning task hand-off)

**Direction:** UC-02 creates `it_provisioning` and `access_management` tasks → UC-04 executes them via ITSM

```python
# [PROPOSED] Input UC-02 gives UC-04 (via task record + O2 orchestrator)
class ProvisioningTask(TaskRecord):
    category: TaskCategory  # it_provisioning | access_management
    standard_kit: list[str]         # [PROPOSED] e.g. ["laptop_std", "email", "vpn"]
    system_access_list: list[str]   # [PROPOSED] e.g. ["jira", "github", "slack"]
    requires_document_clearance: bool = False  # [FROM BLUEPRINT] True for visa-sensitive roles

# [PROPOSED] UC-04 publishes this back to UC-02
class ProvisioningStatusFlag(BaseModel):
    onboarding_id:      str
    itsm_ticket_id:     str
    provisioning_status: str   # "open" | "in_progress" | "complete" | "blocked"
    items_complete:     list[str]
    items_pending:      list[str]
    as_of:              datetime
```

### D.4 UC-02 → UC-05 (orientation task hand-off)

**Direction:** UC-02 creates orientation and acknowledgement tasks → UC-05 manages them

```python
# [PROPOSED] UC-02 creates; UC-05 fills in
class OrientationTask(TaskRecord):
    category = TaskCategory.ORIENTATION
    orientation_date: date | None = None      # [PROPOSED] scheduled by UC-05
    buddy_id: str | None = None               # [PROPOSED] assigned by UC-05 from buddy pool
    handbook_sent: bool = False               # [PROPOSED] UC-05 marks True
    handbook_language: str = "en"             # [PROPOSED] "en" | "ar" | "both"

class Plan30_60_90Task(TaskRecord):
    category = TaskCategory.PLAN_30_60_90
    plan_draft: dict | None = None            # [PROPOSED] UC-05 fills via LLM proposal
    plan_approved: bool = False               # [PROPOSED] HR Lead decision
    approval_id: str | None = None            # [FROM REFERENCE] links to HITLInbox item
```

### D.5 UC-02 → UC-06 (readiness check and joining confirmation)

**Direction:** UC-02 triggers UC-06 when all tasks are COMPLETE (or BLOCKED with exception)

```python
# [PROPOSED] UC-02 produces this as a Proposal with gate = "joining_confirmation"
class OnboardingReadinessReport(BaseModel):
    """Payload in the joining_confirmation Proposal. No PII — IDs and flags only."""
    onboarding_id:       str
    employee_id:         str
    joining_date:        date
    all_tasks_complete:  bool
    blocked_tasks:       list[str]   # task_ids that are BLOCKED with exceptions
    open_tasks:          list[str]   # task_ids still PENDING/IN_PROGRESS (should be empty)
    document_status:     str         # from UC-03 flag
    provisioning_status: str         # from UC-04 flag
    acknowledgement_ref: str | None  # from UC-05 e-sign/click ref
    days_to_joining:     int

# [PROPOSED] UC-06 produces this as a brain_feedback after joining instructions sent
class JoiningConfirmedFeedback(BaseModel):
    type: str = "onboarding.complete"
    onboarding_id: str
    employee_id:   str
    joined_on:     date
```

### D.6 Cross-cutting: O2 orchestrator ↔ UC-02

The O2 orchestrator is the only caller of UC-02 (sub-agents never call each other — CLAUDE.md rule 5). The orchestrator:
- Sends `AgentRequest[OnboardingTrigger]` to UC-02 on `offer.accepted`
- Sends `AgentRequest[OnboardingStatusCheck]` to UC-02 on scheduled polling (e.g. daily)
- Sends `AgentRequest[OnboardingDateChange]` to UC-02 when HR Ops requests a date change
- Receives `AgentResponse` with proposals that go into `HITLInbox`

```python
# [PROPOSED] Three input modes, discriminated by mode field
class OnboardingTaskInput(BaseModel):
    mode: Literal["create", "check_status", "date_change", "cancel"]
    employee_id:     str
    onboarding_id:   str | None = None    # required for all modes except "create"
    joining_date:    date | None = None   # required for "create"
    new_joining_date: date | None = None  # required for "date_change"
    # Fields for "create" mode:
    role_id:     str | None = None
    level:       str | None = None
    department:  str | None = None
    manager_id:  str | None = None
    location:    str | None = None
    emiratisation_applicable: bool = False
```

**All contracts in this section are `[PROPOSED]`.** They must be reviewed and confirmed with the team before implementation.

---

## 3. Reference Architecture Findings
*(Rev 1 content preserved; C.4 above adds the AGENT_HOME annotation)*

### 3.1 Repository layout (from reference)

```
hr-recruitment-<name>-agent/
├── src/
│   └── <package_name>/          ← CRITICAL: must be a proper package, not flat src/
│       ├── __init__.py
│       ├── agent.py
│       └── api.py
├── guardrails/
│   └── policy.yaml
├── evaluation/
│   └── run_evals.py
├── tests/
│   └── test_<name>.py
├── pyproject.toml
├── requirements.txt
├── Dockerfile
├── .env.example
└── README.md
```

> ⚠️ **Target repo gap:** The target repo has `src/agent.py` (flat), not `src/onboarding_task_management/agent.py`. The `resolve_agent_home(__file__)` helper in `config.py` uses `Path(__file__).resolve().parents[2]`, which for `src/<package>/agent.py` gives the agent root. For `src/agent.py` it gives the **parent of the repo**. This will cause `guardrails/policy.yaml` lookups to fail at runtime.

### 3.2 Non-negotiable engineering rules (from CLAUDE.md)

1. One path to a model: `rc.llm(...)` only. Never import provider SDKs in agent code.
2. No real PII in repo. All fixtures from `synthetic.py`.
3. Gates, autonomy, allowlists live in `guardrails/policy.yaml` only.
4. Numbers come from code (dates, due-dates, counts).
5. Untrusted text passes `detect_injection` and is wrapped with `wrap_untrusted`.
6. Protected attributes never reach a prompt.
7. Never auto-act on hard-gated items.
8. Logs carry IDs, hashes, counts only — never prompt text or contact details.

### 3.3 `BaseAgent` pattern (how UC-02 agent must be built)

```python
class OnboardingTaskAgent(BaseAgent[OnboardingTaskInput]):
    agent_id = "onboarding-task-management"   # must match policy.yaml agent: field
    version = "0.1.0"
    input_model = OnboardingTaskInput
    preamble = "..."   # system prompt preamble

    def build_graph(self):
        g = StateGraph(TaskState)
        g.add_node("load_employee", self._load_employee)
        g.add_node("build_task_plan", self._build_task_plan)   # mode=create
        g.add_node("check_status", self._check_status)          # mode=check_status
        g.add_node("handle_date_change", self._handle_date_change)
        g.add_node("handle_alerts", self._handle_alerts)
        # ... conditional edges on mode
        return g.compile()
```

### 3.4 Policy pattern

```yaml
# guardrails/policy.yaml (proposed, not created yet)
policy: onboarding-task-management
agent: onboarding-task-management
version: 1
enabled: true
autonomy: L1
ceiling: L2
tools:
  read:
    - hris.get_employee
    - hris.get_onboarding_record
    - itsm.get_ticket_status
    - brain.search_knowledge
  write: []
  write_requires_approval:
    - hris.update_joining_date
    - itsm.create_ticket
    - hris.record_acknowledgement
gates:
  date_change_approval:
    approver_role: hr_ops
    description: "Joining date change requires HR Ops approval before HRIS is updated"
    sla_hours: 24
  joining_confirmation:
    approver_role: hr_ops
    description: "Joining instructions must not be sent without HR Ops confirmation"
    sla_hours: 4
  plan_30_60_90:
    approver_role: hr_lead
    description: "30/60/90 plan proposal must be approved by HR Lead before sharing"
    sla_hours: 72
never_auto:
  - hris.update_joining_date
  - send_joining_instructions
  - cancel_onboarding
volume_ceiling:
  per_run: 500
thresholds:
  overdue_alert_days_before_joining: 2
  delay_alert_days: 1
approved_by: []   # Required before L1 live: head of HR Ops + DPO names go here
```

---

## 4. Target Repository Status and Gaps

### 4.1 Git status

| Item | Finding |
|---|---|
| Branch | `logesh` |
| Commits | **Zero** — no commits |
| Untracked files | `README.md`, `pyproject.toml`, `src/`, `tests/` |
| Working tree | Clean |

### 4.2 File-level status (updated with Rev 2 findings)

| File / path | Expected | Actual | Priority |
|---|---|---|---|
| `src/onboarding_task_management/__init__.py` | Package marker | **Missing** — current layout is `src/agent.py` (flat) | Critical — breaks `resolve_agent_home` |
| `src/onboarding_task_management/agent.py` | `OnboardingTaskAgent` | **Empty** at wrong path | Critical |
| `src/onboarding_task_management/contracts.py` | Pydantic models | **Empty** at wrong path | Critical |
| `src/onboarding_task_management/config.py` | `AGENT_HOME` + settings | **Empty** at wrong path | Critical |
| `src/onboarding_task_management/synthetic.py` | Fictional onboarding world | **Empty** at wrong path | Critical for tests |
| `src/onboarding_task_management/testing.py` | Test helpers | **Empty** at wrong path | Critical for tests |
| `src/onboarding_task_management/api.py` | FastAPI service | Missing entirely | Medium |
| `tests/test_onboarding.py` | Test suite | **Empty** | Critical |
| `pyproject.toml` | Package metadata + deps | **Empty** | Critical |
| `guardrails/policy.yaml` | Policy envelope | **Missing** | Critical |
| `evaluation/run_evals.py` | Eval harness | **Missing** | Medium |
| `Dockerfile` | Container definition | **Missing** | Low |
| `.env.example` | Env var list | **Missing** | Low |
| `context_layer/skills/` | Skills YAML files | **Missing** | Medium |
| `.python-version` | `3.12` | **Missing** | Low (but prevents `uv` from pinning Python) |

---

## 5. Proposed UC-02 Architecture and Data Contracts
*(Rev 1 content preserved. Full contracts now in §D above.)*

### 5.1 Task data model

```python
# src/onboarding_task_management/contracts.py

from enum import StrEnum
from datetime import date, datetime
from pydantic import BaseModel, Field
from hr_recruitment_common.contracts import new_id, now_utc  # [FROM REFERENCE CODE]

class TaskStatus(StrEnum):
    PENDING     = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE    = "complete"
    OVERDUE     = "overdue"
    BLOCKED     = "blocked"
    CANCELLED   = "cancelled"

class TaskOwner(StrEnum):
    HR_OPS    = "hr_ops"
    IT_MANAGER = "it_manager"
    HR_LEAD   = "hr_lead"
    EMPLOYEE  = "employee"
    BUDDY     = "buddy"
    MANAGER   = "manager"

class TaskCategory(StrEnum):
    HR_RECORD              = "hr_record"
    DOCUMENT_COLLECTION    = "document_collection"
    IT_PROVISIONING        = "it_provisioning"
    ACCESS_MANAGEMENT      = "access_management"
    ORIENTATION            = "orientation"
    POLICY_ACKNOWLEDGEMENT = "policy_acknowledgement"
    PLAN_30_60_90          = "plan_30_60_90"
    JOINING_CONFIRMATION   = "joining_confirmation"

class TaskRecord(BaseModel):
    task_id:       str = Field(default_factory=lambda: new_id("task"))
    employee_id:   str           # canonical HRIS ID — no PII name
    onboarding_id: str
    category:      TaskCategory
    title:         str           # EN
    title_ar:      str = ""      # AR (bilingual requirement)
    owner:         TaskOwner
    status:        TaskStatus = TaskStatus.PENDING
    due_date:      date
    depends_on:    list[str] = Field(default_factory=list)
    itsm_ticket_id: str | None = None
    completed_at:  datetime | None = None
    notes:         str = ""
    created_at:    datetime = Field(default_factory=now_utc)
    updated_at:    datetime = Field(default_factory=now_utc)

class OnboardingRecord(BaseModel):
    onboarding_id: str = Field(default_factory=lambda: new_id("onb"))
    employee_id:   str
    joining_date:  date
    role_id:       str
    level:         str
    department:    str
    manager_id:    str
    tasks:         list[TaskRecord] = Field(default_factory=list)
    status:        str = "active"
    created_at:    datetime = Field(default_factory=now_utc)
    updated_at:    datetime = Field(default_factory=now_utc)

class TaskSummary(BaseModel):
    """Proposal payload — IDs and counts only, no PII."""
    onboarding_id:    str
    employee_id:      str
    total_tasks:      int
    complete:         int
    pending:          int
    overdue:          int
    blocked:          int
    days_to_joining:  int
    overdue_task_ids: list[str]
    is_on_track:      bool
```

### 5.2–5.8 (Rev 1 content — task state machine, ownership, due-date rules, parallel execution, date-change handling, delay alerts, audit events — preserved without change)

---

## 6. Synthetic Data and Test Plan
*(Rev 1 content preserved)*

### 6.1 Fictional employee personas (12, no real PII)

| ID | Name | Role | Joining | Scenario |
|---|---|---|---|---|
| `EMP-001` | Fatima Al Hammadi | Backend Engineer L3 | T+14 | Happy path |
| `EMP-002` | Arjun Mehta | Data Engineer L3 | T+10 | Document delay |
| `EMP-003` | Sophie Martin | ML Engineer L4 | T+21 | IT delay |
| `EMP-004` | Omar Haddad | DevOps Engineer L3 | T+7 | Joining date moved |
| `EMP-005` | Priya Nair | QA Automation L2 | T+14 | Employee withdrawal |
| `EMP-006` | Ahmed Al Mansoori | Frontend Engineer L2 | T+14 | Offer rescinded |
| `EMP-007` | James Carter | Data Engineer L3 | T+7 | Duplicate onboarding |
| `EMP-008` | Mariam Al Ketbi | Backend Engineer L3 | T+14 | Parallel success |
| `EMP-009` | Youssef Mahmoud | Backend Engineer L2 | T+14 | Plan rejection loop |
| `EMP-010` | Noura Al Suwaidi | ML Engineer L3 | T+14 | Unauthorized access |
| `EMP-011` | Lina Saleh | DevOps Engineer L2 | T+5 | Short-notice joining |
| `EMP-012` | Hassan Darwish | Data Engineer L3 | T+14 | Arabic employee (bilingual) |

---

## 7. Missing Information and Later Access Checklist (Updated Rev 2)

### 7.1 Confirmed requirements

*(All items from Rev 1 remain confirmed — see §A.3 for the complete verified list.)*

### 7.2 Items classified (Rev 2 update)

Each item from Rev 1 §7.2 is now classified as one of:
- **✅ Available** — present in an inspected file, no action needed
- **🔍 Needs confirmation** — exists in some form but must be agreed/scoped with the team
- **❌ Missing** — not found in any repo, cannot proceed without it
- **⏳ Later only** — not needed for local synthetic-data development; needed before L1 live

| # | Item | Classification | Evidence / Notes |
|---|---|---|---|
| C-1 | UC-01 through UC-06 specification document | ❌ **Missing** | Not found in either repo after full directory search. Inferred from blueprint only. **Cannot begin implementation without this.** |
| C-2 | Common package strategy (standalone vs. import) | 🔍 **Needs confirmation** | Option A (import) strongly recommended (§B). Decision required from AI Architect / repo owner before `pyproject.toml` can be written. |
| C-3 | HRIS connector (product, API type, sandbox) | ⏳ **Later only** | Not needed for local dev with `MockHRIS`. Required before live integration. |
| C-4 | ITSM connector (product, API, ticket schema) | ⏳ **Later only** | Not needed for local dev with `MockITSM`. Required before live integration. |
| C-5 | Agent ID registered in `dact-business-framework` | 🔍 **Needs confirmation** | Proposed `"onboarding-task-management"`. Must match `policy.yaml agent:` field and any CI registration check. |
| C-6 | Onboarding checklist content (tasks per role/function) | 🔍 **Needs confirmation** | Referenced in wave-2-plan K2-4. Content owner: HR Ops. Can use placeholder task list for local dev, but task titles will be incomplete. |
| C-7 | UAE public holiday calendar | ✅ **Available (partial)** | UAE Labour Law specifies official holidays. A static list can be hardcoded for 2026-2027. Full accuracy requires Legal sign-off on the list. |
| C-8 | Email / notification connector (MS Graph or equivalent) | ⏳ **Later only** | Not needed for local dev. Required for UC-06 sending joining instructions before L1 live. |
| C-9 | E-sign connector (DocuSign / Adobe Sign / UAE Pass) | ⏳ **Later only** | Referenced in wave-2-plan P4. UC-05 acknowledgement can use a `MockESign` for dev. Legal must confirm provider. |
| C-10 | LLM credentials (`ANTHROPIC_API_KEY`) | ⏳ **Later only** | Not needed for offline tests with `FakeLLM`. Required for live evals and 30/60/90 plan drafting. Use dedicated HR workspace (wave-1-runbook §5.6). |
| C-11 | Target autonomy confirmation (L2 in scope?) | 🔍 **Needs confirmation** | Blueprint says "L1 → L2 for checklists, tickets" for A10. Policy ceiling should be L2 even at launch. DPO must co-sign the ceiling. |
| C-12 | `approved_by` names in policy.yaml | ⏳ **Later only** | Required before L1 live (wave-1-runbook G-3). Names of Head of HR Ops + DPO. Not blocking for dev. |
| C-13 | Volume ceiling (onboarding runs per day) | ✅ **Available (estimate)** | Reference policy uses `per_run: 500`. For onboarding, 1 run = 1 employee; a ceiling of `per_day: 50` is a safe start. Confirm with HR Ops but not blocking. |

### 7.3 Additional items identified in Rev 2

| # | Item | Classification | Notes |
|---|---|---|---|
| C-14 | `uv` installation on development machine | ❌ **Missing on local machine** | `uv` is the only supported build/test tool (CLAUDE.md). Must be installed before `uv sync` can run. |
| C-15 | Workspace root `pyproject.toml` for the onboarding monorepo | ❌ **Missing** | Needed to declare the `uv` workspace with both the agent folder and (optionally) `hr-onboarding-common`. |
| C-16 | `.python-version` file | ❌ **Missing** | Reference repo uses `3.12`. Should be present at repo root for `uv` to pin Python. |
| C-17 | A11 cross-agent status flag contract | 🔍 **Needs confirmation** | Blueprint says A11 publishes "status flags only (T4)". The exact flag schema for UC-02 to consume is undefined. |
| C-18 | Buddy pool data source | 🔍 **Needs confirmation** | Blueprint lists buddy assignment as an A10 task. No buddy pool data source identified in wave-2-plan knowledge list. |
| C-19 | Health insurance enrolment task ownership | 🔍 **Needs confirmation** | Wave-2-plan §3.2 mentions mandatory health insurance enrolment as a day-1 dependency. Unclear if UC-02 creates a task or A15 (Leave & Benefits) handles it. |
| C-20 | Package name (Python module) | 🔍 **Needs confirmation** | Proposed `onboarding_task_management` (snake_case of `onboarding-task-management`). Must be agreed before folder creation. |

---

## Section E — Build/Test Boundary: What Can Be Done Now vs. Later

### E.1 What CAN be built and tested locally with synthetic data only

No external credentials, no API access, no MCP integration, no human approval required:

| Work item | Synthetic-data sufficient? | Blocker |
|---|---|---|
| `pyproject.toml` with local path dep to `hr-recruitment-common` | ✅ | C-2 decision needed first |
| `guardrails/policy.yaml` (initial L1, gates defined) | ✅ | C-5 (agent ID) needed |
| `contracts.py` — `TaskRecord`, `OnboardingRecord`, `OnboardingTaskInput`, `TaskSummary` | ✅ | None |
| Deterministic date calculator (UAE business-day logic, static holiday list) | ✅ | None — highest-confidence first step |
| `synthetic.py` — `build_onboarding_world()` with 12 fictional employees | ✅ | None |
| `testing.py` — `build_onboarding_context()`, `make_onboarding_request()` | ✅ | Needs C-2 decision |
| `agent.py` — `OnboardingTaskAgent` with `LangGraph` graph (all nodes) | ✅ (with `FakeLLM` and `MockHRIS`/`MockITSM`) | C-2, C-5 |
| Task plan builder (deterministic: role → task list → due dates) | ✅ | C-6 (task list content) |
| Task status checker and overdue detector | ✅ | None |
| Delay alert batching logic | ✅ | None |
| Joining-date change recalculation | ✅ | None |
| `tests/test_onboarding.py` — happy path, task creation, status | ✅ | None |
| `tests/test_policy.py` — kill switch, tool allowlist, volume ceiling | ✅ | None |
| `tests/test_hitl.py` — gate enforcement, unauthorized actor | ✅ | None |
| `tests/test_date_recalculation.py` — UAE calendar, edge cases | ✅ | None |
| `tests/test_alerts.py` — overdue detection, suppression | ✅ | None |
| `tests/test_idempotency.py` — duplicate runs | ✅ | None |
| `tests/test_bilingual.py` — AR task titles (with `FakeLLM` handler) | ✅ | None |
| `tests/test_api.py` — FastAPI endpoints via `TestClient` | ✅ | C-2 |
| `evaluation/run_evals.py` — offline eval with `FakeLLM` | ✅ | None |
| `context_layer/skills/` — `SKILL.md` files for task-plan and 30/60/90 skills | ✅ | None |

### E.2 What CANNOT be built or tested without external access

| Work item | Blocker | Required before |
|---|---|---|
| Real `HRISPort` implementation | C-3 (HRIS product + sandbox) | L1 live |
| Real `ITSMPort` implementation | C-4 (ITSM product + sandbox) | L1 live |
| Live-model 30/60/90 plan quality tests | C-10 (LLM API key) | Shadow mode |
| Live-model bilingual task title generation | C-10 | Shadow mode |
| Policy envelope sign-off (`approved_by` filled) | C-12 | L1 live |
| UC-06 sending joining instructions | C-8 (email connector) | L1 live |
| UC-05 e-sign acknowledgement | C-9 (e-sign connector) | L1 live |
| A11 status flag consumption | C-17 (A11 contract) | L1 live |
| Real `HITLInbox` with multiple approvers | C-12 (identity, roles) | L1 live |
| Emiratisation tracking (A26 integration) | Wave 2 exit criteria | Wave 3 |

### E.3 Recommended first three days of work (after blockers B-1 and B-2 resolved)

```
Day 1 (no code, just scaffold):
  1. Install uv (C-14)
  2. Create .python-version = 3.12
  3. Create workspace root pyproject.toml (uv workspace)
  4. Create pyproject.toml for the onboarding agent (with hr-recruitment-common path dep)
  5. Create src/onboarding_task_management/__init__.py
  6. Run: uv sync → confirm all deps resolve
  7. Create guardrails/policy.yaml (L1, 3 gates, no write tools yet)

Day 2 (contracts + date calculator — pure Python, no deps on agent harness):
  1. Write contracts.py (TaskRecord, OnboardingRecord, OnboardingTaskInput, TaskSummary)
  2. Write date calculator skill: uae_business_days(start, n, holiday_list) → date
  3. Write tests/test_date_recalculation.py (12 edge cases including UAE holidays, year-end)
  4. Run: uv run pytest tests/test_date_recalculation.py → all pass

Day 3 (synthetic world + tests scaffold):
  1. Write synthetic.py → build_onboarding_world() (12 fictional employees, mock tasks)
  2. Write testing.py → build_onboarding_context(), make_onboarding_request()
  3. Write tests/test_policy.py (kill switch, allowlist — these pass before agent.py exists)
  4. Write empty agent.py with class skeleton + AGENT_HOME resolution
  5. Run: uv run pytest → confirm all non-agent tests pass
```

---

## 8. Risks, Unresolved Questions, and Development-Readiness Decision

### 8.1 Updated risk register

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R-1 | Missing UC spec causes scope rework | High | High | Obtain spec before any implementation |
| R-2 | Common-package arch unclear | High | High | Decide Option A vs B this sprint (§B recommendation) |
| R-3 | HRIS/ITSM connectors unavailable | High | Medium | `MockHRIS`, `MockITSM` satisfy offline dev |
| R-4 | UAE date math complexity | Medium | High | Build date calculator first, fully unit-tested |
| R-5 | Duplicate onboarding runs | Medium | Medium | `AgentRequest.idempotency_key` already in contracts |
| R-6 | Bilingual AR+EN task generation | Medium | Medium | `FakeLLM` handler for offline; live eval needs C-10 |
| R-7 | Gate bypass through parallelism | Low | High | `HITLInbox` has `threading.RLock`; atomic read-check-write |
| R-8 | PII in logs | Low | High | CLAUDE.md rule 8; `EpisodicMemory._scrub` for memory |
| R-9 | `src/agent.py` flat layout breaks `resolve_agent_home` | **Confirmed** | High | Rename to `src/onboarding_task_management/agent.py` immediately |
| R-10 | `uv` not installed on dev machine | **Confirmed** | High | Install `uv` (C-14) before any `uv sync` |
| R-11 | A11 cross-agent status flag contract undefined | Medium | Medium | Placeholder flag in synthetic.py; confirm C-17 before live |
| R-12 | `Scope.requisition_ids` misuse for onboarding scope | Low | Low | Document the convention; fix if Scope model is extended |

### 8.2 Decisions needed before implementation can begin

| # | Decision | Options | Recommended | Who decides |
|---|---|---|---|---|
| D-1 | Obtain UC-01–UC-06 specification document | Spec exists / will be written / use blueprint inference | **Get the spec** | Product owner / HR lead |
| D-2 | Common package strategy | Option A (path dep) / Option B (duplicate) | **Option A** (§B) | AI Architect / repo owner |
| D-3 | Repo structure: merge into monorepo or stay separate | Merge / stay separate + private package index | **Decide now** to avoid multiple pyproject changes | AI Architect |
| D-4 | Install `uv` on local machine | — | **Do it first** | Developer (Logesh) |
| D-5 | Confirm Python package name | `onboarding_task_management` vs other | `onboarding_task_management` | Team |
| D-6 | Confirm agent ID | `onboarding-task-management` vs other | `onboarding-task-management` | AI Architect / framework team |
| D-7 | Confirm `src/` layout (flat vs. proper package) | Fix now / keep flat | **Fix now** (prevents `AGENT_HOME` breakage) | Developer (Logesh) |

### 8.3 Development-readiness decision

**Verdict: NOT READY to begin implementation. Two hard blockers remain. Three pre-implementation actions are executable immediately.**

| Status | Item |
|---|---|
| 🔴 **Hard blocker** | B-1: UC specification document not found |
| 🔴 **Hard blocker** | B-2: Common package architecture undecided |
| 🟡 **Executable now (no external access)** | Install `uv` (C-14) |
| 🟡 **Executable now** | Create `.python-version = 3.12` |
| 🟡 **Executable now** | Rename `src/agent.py` → `src/onboarding_task_management/agent.py` (fix AGENT_HOME layout) |

Once B-1 and B-2 are resolved, implementation follows the sequence:

```
Week 1:  D-4 (install uv) → workspace pyproject → guardrails/policy.yaml → contracts.py
Week 1:  UAE date calculator → tests/test_date_recalculation.py
Week 2:  MockHRIS, MockITSM → synthetic.py → testing.py
Week 3:  agent.py LangGraph graph (create mode only) → tests/test_onboarding.py
Week 3:  tests/test_policy.py, tests/test_hitl.py, tests/test_alerts.py
Week 4:  evaluation/run_evals.py (offline, FakeLLM) → api.py (create_app)
Week 4:  tests/test_api.py, tests/test_bilingual.py, tests/test_idempotency.py
Week 5+: Integration with real HRIS/ITSM when sandboxes available (E.2)
```

---

## 9. Appendix: Reference Repository — Complete Module Inventory

| Path | Purpose |
|---|---|
| `hr-recruitment-common/src/hr_recruitment_common/agent.py` (198 lines) | BaseAgent, AgentContext, RunContext |
| `hr-recruitment-common/src/hr_recruitment_common/api.py` (146 lines) | FastAPI factory (create_app), RunBody, DecisionBody |
| `hr-recruitment-common/src/hr_recruitment_common/config.py` (80 lines) | Settings, resolve_agent_home, PACKAGE_DATA |
| `hr-recruitment-common/src/hr_recruitment_common/contracts.py` (156 lines) | All shared Pydantic contracts |
| `hr-recruitment-common/src/hr_recruitment_common/synthetic.py` | Fictional UAE world |
| `hr-recruitment-common/src/hr_recruitment_common/testing.py` | build_context, make_request, SampleAgent |
| `hr-recruitment-common/src/hr_recruitment_common/connectors/ats/base.py` (89 lines) | ATSPort Protocol, Candidate, Application, Requisition, Stage |
| `hr-recruitment-common/src/hr_recruitment_common/context_layer/__init__.py` | Brain dataclass (ontology + knowledge + memory) |
| `hr-recruitment-common/src/hr_recruitment_common/context_layer/memory.py` (60 lines) | EpisodicMemory (SQLite, RLock, DSAR delete, scrub) |
| `hr-recruitment-common/src/hr_recruitment_common/context_layer/knowledge.py` (86 lines) | KnowledgeIndex (BM25, tier filter, AR normalised) |
| `hr-recruitment-common/src/hr_recruitment_common/context_layer/ontology.py` (145 lines) | Ontology (skills, roles, panels, adjacency) |
| `hr-recruitment-common/src/hr_recruitment_common/llm/__init__.py` | LLM exports |
| `hr-recruitment-common/src/hr_recruitment_common/llm/base.py` (113 lines) | LLMClient Protocol, FakeLLM, ModelTier, BaseLLM |
| `hr-recruitment-common/src/hr_recruitment_common/llm/gateway.py` (7 KB) | LiteLLMGateway (retry, fallback, structured output, caching) |
| `hr-recruitment-common/src/hr_recruitment_common/llm/callbacks.py` | LLMEvent, LLMCallback |
| `hr-recruitment-common/src/hr_recruitment_common/platform/audit.py` (46 lines) | AuditLog (JSONL, DSAR redact) |
| `hr-recruitment-common/src/hr_recruitment_common/platform/hitl.py` (169 lines) | HITLInbox, ApprovalItem, Decision, ReasonCode |
| `hr-recruitment-common/src/hr_recruitment_common/platform/policy.py` (101 lines) | Policy, PolicyStore, GateDef, PolicyViolation |
| `hr-recruitment-common/src/hr_recruitment_common/platform/tools.py` (48 lines) | ToolGateway (allowlist, approval, audit, OTel) |
| `hr-recruitment-common/src/hr_recruitment_common/platform/guardrails.py` (201 lines) | Injection detection, PII redaction, pay figures, inclusive language |
| `hr-recruitment-common/src/hr_recruitment_common/platform/skills.py` (47 lines) | SkillLibrary (SKILL.md front-matter loader, compose) |
| `hr-recruitment-common/src/hr_recruitment_common/observability/` | OTel tracing (span), structured logging |
| `hr-recruitment-common/pyproject.toml` | `hr-recruitment-common 0.1.0`, 13 runtime deps, hatchling build |
| `hr-recruitment-candidate-matching-agent/pyproject.toml` | Agent pkg; one dep: `hr-recruitment-common`; `[tool.uv.sources]` workspace |
| `hr-recruitment-candidate-matching-agent/requirements.txt` (311 lines) | Full locked dep tree (uv-exported); reference for onboarding lockfile |
| `hr-recruitment-candidate-matching-agent/src/candidate_matching_agent/agent.py` (152 lines) | Reference LangGraph agent (StateGraph, 3 nodes, deterministic) |
| `hr-recruitment-candidate-matching-agent/src/candidate_matching_agent/api.py` (12 lines) | Reference FastAPI app (create_app) |
| `hr-recruitment-candidate-matching-agent/guardrails/policy.yaml` | Reference policy envelope (L0 launch, L1 ceiling) |
| `hr-recruitment-candidate-matching-agent/evaluation/run_evals.py` (63 lines) | Reference eval (offline, thresholds dict, exit code 1 on failure) |
| `hr-recruitment-candidate-matching-agent/tests/test_candidate_matching.py` (95 lines) | Reference test suite (9 tests, FakeLLM, TestClient) |
| `docs/hr-agentic-ai-blueprint.md` (917 lines) | Architecture blueprint |
| `docs/wave-1-runbook.md` (310 lines) | Wave 1 operational runbook |
| `docs/wave-2-plan.md` (163 lines) | Wave 2 plan (A10 onboarding) |
| `docs/adr/ADR-001` through `ADR-005` | Architecture decision records |
| `CLAUDE.md` | Engineering non-negotiables (8 rules) |

---

*End of pre-development review (Revision 2). No application code was written, modified or deleted. No packages were installed. No git operations were performed. No APIs were called. No external systems were accessed.*
