# UC-04 Equipment & Approved Access Management: integration handover

Package for the integration team. **Local simulation only**: every IT desk, case registry, approval desk and Outlook connector is a mock, there is no LLM, and all identifiers are synthetic (`SYN-...`). Nothing here is production-ready.

Owner of UC-04: Roshan. Branch `Roshan`. Use case numbering ("UC-04") is the team's label; the Enterprise HR Agent introduction deck and the Onboarding Business Framework do not define it. In the deck, equipment and approved access is a capability of **Agent 6, Onboarding**.

## 1. Read this first

| Topic | Where |
|---|---|
| What it does, how to run it, verified results | `README.md` |
| Engineering rules | `CLAUDE.md` |
| Specification, open questions, limitations | `specs/UC04_SPEC.md`, `specs/TRACEABILITY.md` |
| Where each real integration plugs in | `docs/INTEGRATION_BOUNDARIES.md` |
| Business values still unconfirmed | `docs/CONFIG_GAP_REPORT.md` |
| Proposed database fields (specification only, not approved) | `docs/DB_SCHEMA_SPEC.md` |
| Local API and Swagger scenarios | `docs/LOCAL_API.md`, `docs/SWAGGER_TEST_CATALOG.md` |

## 2. The mandatory sub-agent structure

| Required | In this package | Note |
|---|---|---|
| `connectors/outlook/` (`__init__.py`, `base.py`, `mock.py`) | Present | Port plus mock. No real Microsoft Graph code |
| `context_layer/skills/` | Present | 5 advisory skills, format in its README |
| `evaluation/run_evals.py` | Present | 12 scenarios against `expected_outputs.json`, both entry points |
| `guardrails/policy.yaml` | Present, **not enforced** | Documentation mirror. The enforced policy is `policy/uc04_policy.json` (see section 6) |
| `src/sub_agent/__init__.py`, `agent.py`, `fakes.py` | Present | |
| `src/sub_agent/api.py` | **Is a package**: `src/sub_agent/api/` | `from sub_agent.api import app` works. A file and a package cannot share a name (see section 6) |
| `tests/testcase.py` | Present | |
| `.env.example`, `pyproject.toml` | Present | |

## 3. Additional files and modules UC-04 needs

| Group | Files | Why |
|---|---|---|
| Business engine | `src/sub_agent/service.py`, `contracts.py`, `states.py`, `rules.py`, `items.py`, `approvals.py`, `evidence.py`, `lifecycle.py`, `risk.py`, `readiness.py`, `calendar.py`, `store.py`, `audit.py`, `ports.py`, `config_state.py`, `guardrails.py`, `policy.py`, `fixtures.py`; `adapters/`, `entrypoints/` | All business rules live in `Uc04Service`. Everything else calls it |
| Advisory layer | `advisor.py`, `knowledge.py`, `skills.py`, `brain/onboarding/uc04_equipment_access.md` | The simulated (Fake LLM) read-only explanation |
| Policy and tools | `policy/uc04_policy.json`, `guardrails/guardrails.py`, `mcp/` | Enforced policy and kill switch, privacy checks, local MCP server with 4 tools |
| Local API | `src/sub_agent/api/`, `run_api.py` | Swagger UI over the same service, local only, no authentication |
| Runners | `run_demo.py`, `run_agent.py` | Narrated demo and terminal runner |
| Data | `data/` | Synthetic client configs and cases |
| Packaging | `Dockerfile`, `.dockerignore`, `.github/workflows/ci.yml`, `.gitignore` | Local-only image and CI. Neither has been run on Docker or GitHub |

## 4. How UC-04 is called

UC-04 does not talk to other agents. The parent onboarding orchestrator calls it with one envelope and receives one response. People approve on Teams; approvals come back inside the next call's `input`.

```
Orchestrator --OrchestratorRequest--> OrchestratedEntry --> Uc04Service --> mock IT desk / case registry / approval desk
Orchestrator <--OrchestratorResponse-- (proposals, exceptions, full result)
```

Request (`src/sub_agent/orchestrated.py`; the same call is `POST /api/v1/sub_agent/orchestrated/evaluate`). The `workflow.id` and `step` values are examples:
```json
{
  "contract_version": "1.0",
  "run_id": "run-001",
  "idempotency_key": "case-001-eval-1",
  "workflow": { "id": "O1", "instance": "SYN-CASE-001", "step": "equipment_access" },
  "actor": { "on_behalf_of": "SYN-USER-100", "roles": ["hr_onboarding_officer"] },
  "input": { "case_id": "SYN-CASE-001", "client_id": "client_alpha", "role_id": "software_engineer", "joining_date": "2026-11-16",
             "offer_accepted": true, "onboarding_form_approved": true, "requested_access": [], "requested_equipment": [],
             "config": {}, "approval_records": [], "...": "see Uc04Input in src/sub_agent/contracts.py" }
}
```
Response:
```json
{ "run_id": "run-001", "status": "proposed",
  "proposals": [ { "type": "...", "subject_id": "...", "payload": {"case_id": "..."}, "rationale": "...", "requires_approval_by": "..." } ],
  "exceptions": [ { "code": "...", "detail": "...", "subject_id": "..." } ],
  "result": { "overall_status": "AT_RISK", "items": [], "blockers": [], "risks": [], "next_actions": [] } }
```

| Rule | Behaviour |
|---|---|
| `contract_version` | Only `"1.0"`; any other value is refused before any work |
| `workflow.instance` | Must equal `input.case_id`. UC-04 never creates or renumbers a case |
| Caller roles | `hr_onboarding_officer`, `hiring_manager` or `onboarding_orchestrator` |
| `status` | `exception` when the case is BLOCKED, otherwise `proposed`. Never `acted` locally |
| Idempotency | Same key and same payload returns the stored result while the case state and parent status are unchanged. A different payload under the same key is a conflict |
| Writes | Only fully approved items can be submitted, and only to the mock IT desk. Nothing is sent without a person's approval |

Case statuses: `BLOCKED`, `AWAITING_APPROVAL`, `IN_PROGRESS`, `AT_RISK`, `READY`, `NEEDS_HUMAN_REVIEW`, `CANCELLED`. `READY` needs authoritative completion evidence for every item.

## 5. Layout against the reference agent layout

Compared with `dact-core-functions-hr-sample-reference` (one folder per agent plus a shared package).

| Reference | Here | Status |
|---|---|---|
| `src/<agent_package>/` | `src/sub_agent/` | Present |
| `context-layer/` | `context_layer/skills/`, `brain/` | Present |
| `guardrails/policy.yaml` | `guardrails/policy.yaml` | Present but not read by code |
| `evaluation/` | `evaluation/run_evals.py` | Present |
| `Dockerfile`, `.env.example`, `README.md`, `.dockerignore` | Same | Present. Dockerfile not run |
| `requirements.txt` exported from a lock file | Extras in `pyproject.toml` | No lock file exists |
| `cicd/` | `.github/workflows/ci.yml` | Written, not run on GitHub |
| `hooks/` | `approvals.py`, `audit.py`, `policy.py` | In code, no folder |
| `tools/` | `connectors/`, `src/sub_agent/adapters/`, `ports.py` | Mock only |
| `mcp/` | `mcp/` | Local mock server only |
| `monitoring/`, `kubernetes/`, `optimization/` | None | Not built |

## 6. Points the integration team must decide or confirm

1. **`api.py` or `api/`.** Does the integration loader accept the `api/` package? If it needs a literal `api.py`, the package must be merged into one file.
2. **Policy loading.** The sample reads `guardrails/policy.yaml` in code. Here `policy/uc04_policy.json` is enforced and the YAML is a mirror that a test keeps in step. Decide which becomes authoritative.
3. **Envelope fields.** The envelope copies the reference contract (`contract_version`, `run_id`, `idempotency_key`, `workflow`, `actor`, `input`). It has none of the reference's `scope`, `brain_context`, `policy`, `provenance`, `gate`, `confidence`, `actions_taken` or `brain_feedback`, and exception codes are free strings. `workflow.id` and `step` are accepted unvalidated because the real contract is not available. A contract test against the real schema is needed.
4. **Approvals.** They arrive only as records in `input.approval_records`. There is no approvals inbox.
5. **Real adapters.** Not built: IT service desk (MCP), parent case registry, approval desk, Outlook (Graph), Entra identity, database and durable audit, tracing, the orchestrator, and any real model. The advisory layer is template-based and is not behind an LLM client interface.
6. **Dependencies.** The core needs only `pydantic` and `pyyaml`. The `integration` extra in `pyproject.toml` keeps the planned packages (LangGraph, LangChain, Anthropic, Azure identity, Key Vault and search, OpenTelemetry). Nothing imports them yet. The reference repository routes all model calls through a LiteLLM gateway and bans a direct provider SDK; whether `langchain-openai` and `anthropic` stay is undecided.
7. **Agent id.** `employee-onboarding-uc04` is not confirmed against the business framework's capability map.
8. **Business values.** Role and equipment matrices, lead times, calendar, evidence rules, ticket statuses and approvers are synthetic. Still open in the source documents: the meaning of HC-08 (spend approval), who approves out-of-matrix exceptions, and whether any item may complete after joining (AC-07).

## 7. Run it

Python 3.12+ (developed on 3.14.4). From the package root:
```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[api,dev]"      (Windows; use .venv/bin/python elsewhere)
python -m pytest
python evaluation/run_evals.py
python run_demo.py
python run_api.py          Swagger UI at http://127.0.0.1:8000/docs
```
