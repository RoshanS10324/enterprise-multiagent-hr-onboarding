# CLAUDE.md: engineering rules for this repository

UC-04 Equipment & Approved Access Management, a deterministic sub-agent of the Enterprise HR Agent Employee Onboarding Agent. Layout, run commands and verified results: [README.md](README.md). Specification and open questions: [specs/UC04_SPEC.md](specs/UC04_SPEC.md).

## Context
- Local simulation only. Every IT desk, case registry and approval desk is a mock. Identifiers are synthetic (`SYN-...`).
- Autonomy L1, ceiling L1 (observe and propose). Nothing is written or sent without a person's approval. The enforced policy is `policy/uc04_policy.json`; `guardrails/policy.yaml` is a documented mirror that no code reads (`tests/test_policy_yaml_mirror.py` keeps the two in step).
- The business rules live in `Uc04Service`. Everything else (API, MCP server, advisory layer, agent facade) calls it and adds no business logic.

## Non-negotiable rules
1. **The service decides, nothing else does.** The Fake LLM advisory (`FakeLlmAdvisor`) is read-only and template-based. It never approves, changes a state, creates a ticket or sends a message. Its output is labelled as simulated.
2. **No real personal data.** Fixtures are synthetic. Free text passes the privacy checks in `guardrails/guardrails.py`; a violation fails closed and the stored state is restored.
3. **Numbers, dates and statuses come from code.** A model or a template never invents them.
4. **Untrusted text** (item notes and other free text) is flagged and treated as data. It is never acted on and never echoed.
5. **Evidence is specific.** An approval, a ticket, a completed ticket, delivery and collection are never proof of one another. `READY` needs authoritative completion evidence for every item.
6. **Writes need approval.** `it_service_desk.submit_request` is a `write_requires_approval` tool. The kill switch (`enabled` in `policy/uc04_policy.json`) must stay enforced and tested.
7. **No network, cloud or model library in the core.** `tests/test_packaging.py` fails if one is imported outside `tests/`. Real integrations (Entra, MCP desk, database, orchestrator, LLM) are separate, approved steps behind the ports in `src/sub_agent/ports.py`.
8. **Do not invent business requirements.** Open questions (for example HC-08 spend approval, who approves exceptions, post-joining completion) stay open in the spec until the business answers.
9. **Reference documents stay outside this repository.** No PDF, DOCX, ZIP or `.git` folder inside the project (TC-39).
10. **Documents must match the code.** The brain file and the skills are checked against the code's own vocabulary (`tests/test_knowledge_accuracy.py`), and the Swagger catalog is generated from the scenario registry (`tests/test_api_scenarios.py`).

## Commands
Python 3.12+. From the repository root (Windows PowerShell shown; use `.venv/bin/python` elsewhere):
- `python -m venv .venv`
- `.\.venv\Scripts\python.exe -m pip install -e ".[api,dev]"`
- `.\.venv\Scripts\python.exe -m pytest`
- `.\.venv\Scripts\python.exe evaluation\run_evals.py` (12 scenarios vs `evaluation/expected_outputs.json`, both entry points)
- `.\.venv\Scripts\python.exe run_demo.py`
- `.\.venv\Scripts\python.exe run_api.py` (local API and Swagger UI on 127.0.0.1:8000, no authentication)
- `.\.venv\Scripts\python.exe -m ruff check --select F,E9 src connectors mcp policy guardrails evaluation`

## When you change something
- Business rule: change `src/sub_agent/`, add or update a test first, then update `specs/UC04_SPEC.md` and `specs/TRACEABILITY.md`.
- New risk, state or code: update `brain/onboarding/uc04_equipment_access.md` and any skill that names it, or `tests/test_knowledge_accuracy.py` fails.
- New API scenario: add it to `src/sub_agent/api/scenarios.py` and regenerate `docs/SWAGGER_TEST_CATALOG.md`.
- New skill: add the Markdown file to `context_layer/skills/` and list it in that folder's README.
