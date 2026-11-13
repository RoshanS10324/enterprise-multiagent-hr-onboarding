# UC-04 local development API (Swagger UI)

**Local development and testing only. Not a production API.** It has no authentication, keeps all state in memory, and talks only to
the same mocks the tests use (mock IT service desk, mock parent case registry). No real ticket, account, licence or employee record
exists or can be created. No LLM is involved. All identifiers are synthetic (`SYN-...`).

The API adds no business logic. Each call goes through the existing `DirectEntry` or `OrchestratedEntry` into `Uc04Service`.

## Start it (Windows PowerShell, from the repository root)
```powershell
python -m venv .venv                                    # once
.\.venv\Scripts\python.exe -m pip install -e ".[api,dev]"      # pydantic, pyyaml, fastapi, uvicorn, pytest, httpx
.\.venv\Scripts\python.exe run_api.py                   # binds to 127.0.0.1:8000 only
```
Stop it with Ctrl+C.

| URL | What |
|---|---|
| http://127.0.0.1:8000/docs | Swagger UI (its page assets load from a CDN, so the browser needs internet; the API itself makes no outbound calls) |
| http://127.0.0.1:8000/openapi.json | OpenAPI schema |
| http://127.0.0.1:8000/health | Health, policy status |
| http://127.0.0.1:8000/ | API information |

## Endpoints
| Method and path | Calls | Notes |
|---|---|---|
| `POST /api/v1/sub_agent/evaluate` | `DirectEntry.evaluate` | Returns `Uc04Result`. Never calls the IT desk. |
| `POST /api/v1/sub_agent/evaluate-with-advisory` | `DirectEntry.evaluate`, then `FakeLlmAdvisor` | Returns the same authoritative `result` plus a simulated, read-only `advisory` (complete multi-section text, labelled as simulated). Same kill switch, role check, strict input and replay rules as `evaluate`. |
| `POST /api/v1/sub_agent/submit-eligible` | `DirectEntry.submit_eligible` | Evaluates, then submits only fully approved items to the **mock** desk. Returns `SubmissionReport`. |
| `POST /api/v1/sub_agent/orchestrated/evaluate` | `OrchestratedEntry.evaluate` | Parent-workflow envelope; `workflow.instance` must equal `input.case_id`. Returns `OrchestratorResponse`. |
| `GET /api/v1/dev/mock-parent-cases` | mock registry (DEV ONLY) | Lists the four synthetic parent cases. |
| `PUT /api/v1/dev/mock-parent-cases/{case_id}/status` | mock registry (DEV ONLY) | Sets `ACTIVE`, `ON_HOLD`, `WITHDRAWN` or `CANCELLED` (mock only). |
| `POST /api/v1/dev/reset` | in-memory state | DEV ONLY. Clears cases, mock tickets and audit. Examples normally reset themselves. |

Request body for the first two: `actor_id`, `roles`, optional `run_id`, optional `idempotency_key`, optional DEV ONLY `dev_reset_first`, and `input` (the exact `Uc04Input`
contract; open `/docs` and expand the `Uc04Input` schema). Pick an example from the **Examples** drop-down in Swagger.

Errors are JSON `{"error": CODE, "detail": ..., "fields": [...]}`. Invalid input returns **field names only, never values**.
| HTTP | error | When |
|---|---|---|
| 403 | `CALLER_NOT_PERMITTED` | Role not allowed by `policy/uc04_policy.json`, or the policy kill switch is off |
| 404 | `UNKNOWN_CASE` | The case is not in the parent registry. UC-04 never creates one. |
| 409 | `CASE_MISMATCH`, `DUPLICATE_CASE`, `IDEMPOTENCY_CONFLICT` | Employee or client differs from the parent case; the employee already has a case; same idempotency key with a different payload |
| 422 | `INVALID_REQUEST`, `PRIVACY_GUARD` | Contract violation; sensitive content would be returned |
| 500 | `INTERNAL_ERROR` | Unexpected error. No details are returned. |

## Swagger as a testing console
Swagger groups HTTP endpoints, not business capabilities. UC-04 has three business endpoints, so each is listed under every
category it can demonstrate (the same operation, not a duplicate): `evaluate` appears under 01, 02, 03, 04, 05, 07 and 08,
`submit-eligible` under 05, and `orchestrated/evaluate` under 06 and 07. The 8 tags are:
`01 - Basic Agent Evaluation`, `02 - Approvals & Exceptions`, `03 - Equipment & Access Rules`, `04 - Lifecycle & Readiness`,
`05 - IT Service Desk Simulation`, `06 - Orchestration`, `07 - Security & Policy`, `08 - State & Idempotency`.

Use the **Examples** drop-down in the request body. Labels read `<category> | <title> -> <expected HTTP> <expected result>`, and
each example shows its purpose, input fixture, steps, expected status and result, and what to inspect. There are 64 scenarios;
**docs/SWAGGER_TEST_CATALOG.md** lists all of them (generated from `src/sub_agent/api/scenarios.py`; a test fails if it drifts).

**Isolation (DEV ONLY).** Single-call examples carry `"dev_reset_first": true`, which clears the in-memory mock state first, so
any example can be run after any other. Multi-step scenarios (replay and conflict, ON_HOLD, WITHDRAWN, resubmission, re-assessment)
say which examples to run first and use `dev_reset_first` false. DEV ONLY operations: `dev_reset_first`, `POST /api/v1/dev/reset`,
`GET /api/v1/dev/mock-parent-cases`, `PUT /api/v1/dev/mock-parent-cases/{case_id}/status`. They change only mock state and must never
be exposed publicly; `create_app(dev_tools=False)` removes them and refuses `dev_reset_first`.

**Not testable over HTTP (documented, not faked).** The policy kill switch (edit `policy/uc04_policy.json`, restart, expect 403; covered by
tests); a mock IT desk failure or timeout (only fault-injection tests reach it: tests/test_failures.py and tests/test_api_hardening.py); `PRIVACY_GUARD` 422 and `INTERNAL_ERROR` 500 (the service sanitises first, so
only fault-injection tests reach them); reading ticket status from a desk (it arrives as input). A visible endpoint is not proof of a
tested capability: each scenario is executed by `tests/test_api_scenarios.py` against the real service.
Also not reachable over HTTP: a client change (EC-35) needs the parent registry's client to change, which the mock registry only supports from code (`MockCaseContext.set_client`), so it is tested in `tests/test_client_change.py`; and `Uc04Service.acknowledge_adapter_error` has no route, so an `ADAPTER_ERROR` raised by a failing mock desk can only be cleared by resetting the mock state.

**Input strictness (2026-10-27).** `dev_reset_first` is a strict boolean ("yes" or 1 is refused with 422), and the orchestrator envelope accepts only `contract_version` "1.0" (anything else is refused with 422, field `contract_version`). `workflow.id` and `workflow.step` are not validated because the real orchestrator contract is not available. A replay with the same idempotency key follows a parent status change: after WITHDRAWN or ON_HOLD the same request returns the live answer, not the stored one.

## Tests
```powershell
.\.venv\Scripts\python.exe -m pytest -o addopts= -p no:cacheprovider -W default      # core + API tests
.\.venv\Scripts\python.exe evals\run_evals.py
```
`tests/test_api.py` runs in-process (no server, no sockets) and fails if any non-loopback connection is attempted. It is skipped
automatically when `fastapi` or `httpx` is not installed, so the core setup (pydantic and pytest only) is unchanged.

## Security gaps (by design for local use)
No authentication or authorization; **caller roles and approvals are request-supplied and cannot be verified**; no rate limiting,
TLS or CORS policy; in-memory state shared by all callers and lost on restart; the dev endpoints can change mock state; requests are
processed one at a time. Do not expose this API beyond 127.0.0.1 and do not use real data.
