# UC-01: Employee Record Creation

Part of the Enterprise employee onboarding multi-agent system (Enterprise HR Agent). UC-01 covers the start
gate, duplicate/variant check, case creation (form version 1), discrepancy
flagging, form-version changes, HRIS/payroll record preparation and write,
and the policy/authorization layer that gates every decision. Every
decision is deterministic Python. An optional advisory model (off by
default) can only escalate or draft, and reaches a model through the
central `hr_onboarding_common` gateway
([docs/uc01-llm-gateway.md](docs/uc01-llm-gateway.md)). There are no network
calls to a real HRIS yet. See [CLAUDE.md](CLAUDE.md) for working rules.

**What is real, mocked and blocked:**
[docs/uc01-integration-status.md](docs/uc01-integration-status.md).
Supabase persistence of cases is **not** working yet; that file lists the
exact decisions it needs. Mock employees: [data/mock/README.md](data/mock/README.md).

**Onboarding documents.** `POST /internal/onboarding-documents` reads an
onboarding-form `.docx` (template `onboarding-form-2026`), extracts and
validates it, and passes it to the same case-creation flow. Extraction
never approves a form and never invents a value. See
[docs/document-extraction.md](docs/document-extraction.md),
[docs/employee-record-mapping.md](docs/employee-record-mapping.md),
[docs/mock-document-testing.md](docs/mock-document-testing.md) and
[docs/supabase-integration-status.md](docs/supabase-integration-status.md).

## Running tests and evals

```
uv sync
uv run pytest
uv run python -m evaluation.uc01.runner
```

Without `uv` (PowerShell), from this folder. A single space keeps the
variable set, so a local `.env` cannot point the tests at a real database.
`$env:X=""` would delete it instead.

```powershell
$env:PYTHONPATH = "src"; $env:SUPABASE_URL = " "; $env:SUPABASE_SERVICE_ROLE_KEY = " "
python -m pytest -q
python evaluation/run_evals.py
python -m demo.full_flow
python scripts/seed_mock_employees.py --twice
python scripts/mock_onboarding_documents.py run
python -m demo.uc01_to_uc02_mock_e2e                      # mock extracted data -> UC-01 -> UC-02
python -m demo.uc01_to_uc02_mock_e2e --persistence supabase   # stops BLOCKED until B1-B9 are decided
```

**Mock extraction.** `documents/providers.py` has two providers with one
result type: `DocxExtractionProvider` reads a real form, and
`MockExtractionProvider` loads `data/mock/extracted_employee_data.json` (a
synthetic "what extraction would return" fixture). Both feed
`DocumentIntakeService.process_extraction`, so validation, HR review and
case creation are identical. `scripts/e2e_uc01_to_uc02.py` runs the full
chain (UC-01, read-back, `OnboardingCaseInput`, the orchestrator's
`plan_tasks` adapter, then UC-02) and labels every step PASS, MOCK PASS,
BLOCKED or NOT VERIFIED. UC-01 storage is in-memory (MOCK PASS) until
Supabase is unblocked.

The eval runner plays deterministic end-to-end scenarios (see
[evaluation/uc01/README.md](evaluation/uc01/README.md)) and writes
`evaluation/uc01/results.json`; `uv run pytest` also runs it in-process via
`tests/test_evals.py`, so a plain test run already covers both.

## Verifying the business requirements

```
uv run python scripts/verify_requirements.py            # one line per check
uv run python scripts/verify_requirements.py --verbose  # full execution trace
```

Both modes walk the same case from acceptance email to written record
using the container's own wiring, and check R1-R6 from
[specs/uc01-requirements.yaml](specs/uc01-requirements.yaml) against actual
behaviour. `--verbose` adds a labelled step-by-step trace around every
call - INPUT, PROCESSING, BUSINESS RULE, APPROVAL REQUIRED, APPROVAL
DECISION, STATE CHANGE, EXTERNAL OP, OUTPUT, AUDIT EVENT, ASSERTION RESULT
- built from what the services returned and from state read back out of
the repos, not from canned text. Every external operation is labelled with
the mode read off the gateway actually wired in; today that is always
`SIMULATED` (`StubHrisGateway`), so a green write does **not** mean a Zoho
record exists. Salary values are masked in both modes
(`src/sub_agent/trace.py`).

## Running the API locally

```
uv sync
cp .env.example .env   # APP_ENV=dev works out of the box
uv run uvicorn sub_agent.api.app:app --reload
```

Identity is dev-only header-based auth (a placeholder for Microsoft Entra
ID - see `docs/known-gaps.md`). Human endpoints need:

| Header | Meaning |
| --- | --- |
| `X-User-Id` | the caller's user id |
| `X-User-Role` | one of `HR_OFFICER`, `HR_MANAGER`, `CLIENT_USER`, `AUDITOR` |
| `X-Client-Ids` | comma-separated client ids the caller may see |

The one system endpoint (`POST /internal/onboarding-requests`) instead
needs `X-System-Key` matching `UC01_SYSTEM_API_KEY`.

### Endpoints

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/health` | none |
| POST | `/internal/onboarding-requests` | system key |
| POST | `/internal/onboarding-documents` | system key |
| GET | `/cases/{case_id}/status` | viewer |
| GET | `/cases/{case_id}/audit` | viewer |
| GET | `/audit/integrity` | viewer (`AUDITOR` only) |
| GET | `/approvals/pending` | viewer |
| POST | `/cases/{case_id}/compare` | viewer |
| POST | `/discrepancies/{discrepancy_id}/decision` | viewer |
| POST | `/cases/{case_id}/form-changes` | viewer |
| POST | `/form-changes/{approval_id}/decision` | viewer |
| POST | `/variant-approvals/{approval_id}/decision` | viewer |
| POST | `/cases/{case_id}/hris/prepare` | viewer |
| POST | `/hris-approvals/{approval_id}/decision` | viewer |
| POST | `/cases/{case_id}/hris/write` | viewer |

No response body ever contains a salary, an email, or a discrepancy's
raw value. The per-case audit response also omits the hash-chain fields
(`sequence`, `prev_hash`, `entry_hash`): `sequence` is global across
clients, so chain verification is a separate `AUDITOR`-only endpoint
over the complete log rather than something a per-case read exposes.

## Docker

```
docker build -t uc01-employee-record .
docker run -p 8000:8000 -e APP_ENV=dev uc01-employee-record
```
