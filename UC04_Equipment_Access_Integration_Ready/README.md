# UC-04 Equipment & Approved Access Management (local simulation)

A deterministic capability inside the Enterprise HR Agent Employee Onboarding Agent. It checks equipment and system-access requests for an accepted joiner against the
approved role/client lists, tracks approvals and (mock) IT tickets, and reports readiness, risks, owners and next actions.

**Local simulation only.** No network, no LLM, no real ticket, account, licence, message, order or data. All identifiers are synthetic (`SYN-...`).
This repository root is the UC-04 project: one self-contained package (`src/sub_agent/`) with its own `pyproject.toml`, `Dockerfile` and CI workflow.
(Earlier documents call it `uc04-equipment-access/` with code in `src/uc04/`; those names are historical.) Engineering rules: [CLAUDE.md](CLAUDE.md).
It is separate from the read-only reference repository.

**The reference documents stay outside.** The two source documents (the Enterprise HR Agent introduction PDF and the Onboarding Business Framework DOCX) live in the
sibling `resourse` folder of the workspace. They are reference-only: never copy them, or that folder, into this project, an image, an archive or the official repository.
A test (TC-39) fails if a PDF/DOCX or a `.git` folder appears inside this project.

## One service, two entry points
```
direct caller ----------> src/sub_agent/entrypoints/direct.py ----------+
                                                                         +--> Uc04Service (all business rules)
parent onboarding flow -> src/sub_agent/entrypoints/orchestrated.py ----+        --> mock IT desk, mock parent case registry
```
Both paths run the same checks and give the same business result for equivalent input (TC-33). UC-04 never creates an onboarding case (TC-34).

## Run it
Python 3.12+ (developed on 3.14.4). The core needs only `pydantic` and `pyyaml`; the local API adds `fastapi` and `uvicorn`; the tests add `pytest` and `httpx`.
These are the `dependencies`, `api` and `dev` groups in `pyproject.toml`. A fourth group, `integration` (LangGraph, LangChain, Anthropic, Azure identity, Key Vault and search, OpenTelemetry and others), is kept for the planned integrations; nothing imports it yet and CI and the Docker image do not install it.
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[api,dev]"
.\.venv\Scripts\python.exe -m pytest                   # unit, rule, failure, privacy, two-path, API, advisory and packaging tests
.\.venv\Scripts\python.exe evaluation\run_evals.py     # 12 scenarios vs hand-authored expected_outputs.json, both entry points
.\.venv\Scripts\python.exe run_demo.py                 # narrated walk-through of one synthetic case
```

## Local API (Swagger UI)
`python run_api.py` serves a local development API at http://127.0.0.1:8000/docs over the same service (mock IT desk only, no authentication,
not production-ready). Setup and limits: `docs/LOCAL_API.md`; the 64 documented Swagger scenarios: `docs/SWAGGER_TEST_CATALOG.md`.
Code: `src/sub_agent/api/`; tests: `tests/test_api.py`, `tests/test_api_scenarios.py`, `tests/test_api_hardening.py`, `tests/test_advisory_api.py` (they need `fastapi`, `uvicorn` and `httpx`
and are skipped without them). A `Dockerfile` packages the same local API for local testing only (see its header; not production).

## Pre-integration documents
`docs/PRE_INTEGRATION_REPORT.md` (audit, validation, readiness checklist), `docs/CONFIG_GAP_REPORT.md` (business values still unconfirmed),
`docs/INTEGRATION_BOUNDARIES.md` (where Entra, MCP, storage and the orchestrator plug in; none is built), `docs/DB_SCHEMA_SPEC.md` (proposed
field-level database schema for review; a specification only, no SQL and not approved).

## Layout
| Path | Purpose |
|---|---|
| `src/sub_agent/service.py` | The single business service (`evaluate`, `submit_eligible`) |
| `src/sub_agent/agent.py` | Sub-agent facade integrating `Uc04Service`, `FakeLlmAdvisor`, and `MockOutlookConnector` |
| `src/sub_agent/advisor.py` | Fake LLM advisory layer (`FakeLlmAdvisor`) providing simulated, read-only explanations |
| `src/sub_agent/knowledge.py` | Brain knowledge loader reading structured specifications from `brain/onboarding/` |
| `src/sub_agent/skills.py` | Skill loader and runner dispatching advisory skills from `context_layer/skills/` |
| `src/sub_agent/api/` | Local development HTTP API exposing direct, orchestrated, and advisory endpoints |
| `src/sub_agent/contracts.py`, `states.py`, `config_state.py` | Validated models, enums, configuration tri-state |
| `src/sub_agent/rules.py`, `items.py`, `approvals.py`, `evidence.py`, `lifecycle.py`, `risk.py`, `readiness.py`, `calendar.py` | Deterministic business rules |
| `src/sub_agent/audit.py`, `guardrails.py`, `policy.py`, `store.py`, `ports.py` | Audit with injected clock, privacy checks, policy gateway, idempotent store |
| `src/sub_agent/adapters/` | Mock IT desk, mock parent case registry, mock approval desk |
| `connectors/outlook/` | Outlook connector interface and mock implementation (`MockOutlookConnector`) |
| `mcp/` | Local MCP server (`MCPServer`) and tool definitions exposing UC-04 capabilities |
| `brain/onboarding/` | Traceable, grounded domain knowledge markdown files |
| `context_layer/skills/` | Read-only advisory skills following repository conventions |
| `guardrails/policy.yaml`, `policy/` | Policy configurations (Autonomy L1, kill switch, role gating) |
| `data/` | Synthetic client configuration, base cases, parent cases |
| `evaluation/` | Scenario evaluation (`run_evals.py`) and hand-authored expected outputs (12/12) |
| `specs/` | `UC04_SPEC.md` (specification, open questions, limitations) and `TRACEABILITY.md` |
| `tests/` | 470 tests (core rules, API, advisory, skills, brain, MCP, connectors, packaging) |
| `pyproject.toml`, `CLAUDE.md`, `Dockerfile`, `.dockerignore`, `.github/workflows/ci.yml` | Packaging, engineering rules, local-only API image, CI (tests, evaluation gate, demo, lint). The Dockerfile and CI workflow have not been run on Docker or GitHub yet |

## Statuses
Case: `BLOCKED`, `NEEDS_HUMAN_REVIEW`, `AWAITING_APPROVAL`, `IN_PROGRESS`, `AT_RISK`, `READY`, `CANCELLED` (local proposal; the sources define none).
Item: draft, approval pending, exception pending, approval/exception received, eligible for simulated submission, mock ticket created, IT in progress,
rejected/correction required, provisioning confirmed, equipment delivered, equipment collected, client-site clearance confirmed, item completed, blocked, cancelled.
Approval, a ticket, a completed ticket, delivery and collection are never treated as proof of one another. READY needs authoritative completion evidence for every item.

## Verified results (re-run 2026-10-28, Python 3.14.4, pydantic 2.14.1, pytest 9.1.1, fastapi 0.159.1)
| Check | Result |
|---|---|
| `python -m pytest` | **469 passed, 1 failed**. The failure is `test_tc39_no_source_documents_or_git_data_inside_the_project`: an untracked `UC04_Equipment_Access_Integration_Ready.zip` sits in the project root and TC-39 bans archives. Remove the zip and it passes (not re-run after removal) |
| `python evaluation/run_evals.py` | **12/12 scenarios** match expected outputs across direct and orchestrated paths |
| `python run_demo.py` | Exit code 0; step 6 shows identical business decisions across both entry points |
| `python -m pytest tests/test_api_scenarios.py tests/test_advisory_api.py` | **86 passed**, 0 failed |
| `python -m pytest tests/test_fake_llm.py tests/test_brain_knowledge.py tests/test_skills.py tests/test_mcp.py` | **26 passed**, 0 failed |
| `ruff check --select F,E9` on `src`, `connectors`, `mcp`, `policy`, `guardrails`, `evaluation`, `run_api.py`, `run_demo.py` | **All checks passed**. The full rule set in `pyproject.toml` (line length 100) reports about 750 findings, mostly long lines, and is not enforced yet. Some older test files also have unused imports |
| `pip install -e ".[api,dev]"` in a new virtual environment, then the CI lint and test commands | Install and lint passed; tests as in the first row |
These are results for this local simulation only. They do not show that the wider onboarding acceptance criteria are met (see `specs/TRACEABILITY.md`) or that anything is production ready.

## Known limitations and open questions
See `specs/UC04_SPEC.md` sections 12 and 13. Key ones: no real configuration values exist yet (matrices, approvers, lead times, calendar, evidence rules, ticket statuses,
state names expected by the wider app, the meaning of "HC-08", whether any equipment or access item may complete after joining); the parent orchestrator is not available in the reference repository, so Scenario B is exercised through a test-only harness;
in-memory store (state and audit are lost on restart); no authentication, so approvals, evidence and caller roles are caller-supplied; no tracing. The policy kill switch (`enabled` in `policy/uc04_policy.json`) is enforced and tested.

## Future integration (not built; each needs approval)
`BaseAgent`/LangGraph and FastAPI adapter over `Uc04Service`; MCP server for the IT service desk behind API Management; Cosmos DB state and audit; Teams approval routing;
Entra Agent ID; OpenTelemetry; running the CI workflow and evaluation gate on GitHub (written, not yet run); the remaining Enterprise HR Agent package folders. Connecting the official repository, creating a branch, committing or deploying is a separate step that
needs explicit authorisation and manager approval.
