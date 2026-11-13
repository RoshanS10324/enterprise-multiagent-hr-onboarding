# UC-03 — Document & Information Validation
## Technical Specification

**Version:** 1.0  
**Status:** Implemented  
**Owner:** Enterprise HR Agent Onboarding Agent Team  

---

## 1. Overview

UC-03 is a module within the Enterprise HR Agent Employee Onboarding Agent. It validates documents
submitted by new employees during onboarding, enforces 16 business rules deterministically,
routes exceptions to HR, and maintains a complete audit trail.

The agent **never** makes final compliance decisions autonomously. Every compliance
completion requires an explicit human approval record.

UC-03 is functionally complete as a 17-step validation pipeline orchestrated by LangGraph and served over FastAPI, implementing start-condition enforcement (BR-01), client-specific YAML-driven checklist loading (BR-03), name mismatch detection (BR-06), document expiry checks, readability and required-field validation (BR-05), evidence storage enforcement (BR-09), server-side approval gates for all protected actions (BR-11), and prompt injection guardrails that treat every byte of submitted document text as untrusted data (BR-16); document extraction follows a hybrid approach using PyMuPDF for local PDF parsing with Claude LLM invoked as a fallback when local extraction confidence falls below threshold, ensuring reliable field extraction without silent guessing (BR-07); the system integrates four MCP clients — HRIS, Microsoft Graph, IT Service Desk, and Documents — to retrieve employee records, provision access, raise service tickets, and store evidence, while every action, draft, and decision is written to a fully immutable per-case audit trail (BR-14); the implementation is backed by a passing automated test suite spanning unit tests across all 16 business rules, integration tests against the FastAPI endpoints, and a 26-scenario eval dataset covering the complete validation failure taxonomy.

---

## 2. Scope

### In scope
- Generating a client-specific document checklist after offer acceptance
- Tracking requested, received, missing, and under-review documents
- Validating document type, readability, required fields, expiry dates, name consistency
- Identifying missing, unreadable, expired, mismatched, and uncertain documents
- Routing to HR for review when automated checks are insufficient
- Drafting document reminders and replacement requests (pending HR approval)
- Preparing a visa document pack for the PRO (pending HR approval)
- Enforcing approval gates server-side before any protected action
- Maintaining a complete audit trail per case

### Out of scope
- Determining document authenticity (agent flags; humans decide)
- Final HR compliance decisions
- Submitting documents to government portals
- Executing communications without HR approval
- Writing to external HR systems without HR approval

---

## 3. Architecture

```
FastAPI  →  DocumentValidationService  →  rules.py        (deterministic)
                      │                →  ChecklistService (YAML-driven)
                      │                →  DocumentExtractor (adapter)
                      │                →  guardrails/       (security)
                      │                →  hooks/            (approval + audit)
                      │                →  CaseStore         (state)
                      │                →  AuditStore        (audit trail)
                      ▼
               LangGraph StateGraph
               (workflow.py)
```

### Component responsibilities

| Component | Responsibility |
|---|---|
| `models.py` | Typed input/output contracts (Pydantic v2) |
| `rules.py` | All 16 business rules as pure Python functions |
| `checklist.py` | Reads YAML checklist, filters by context |
| `extraction.py` | Adapter for document OCR/parsing (mock for now) |
| `validator.py` | Orchestrates all components in a 17-step pipeline |
| `workflow.py` | LangGraph graph — controls flow between steps |
| `guardrails/` | Access control, injection scanning, PII classification |
| `hooks/` | Audit logging, approval gate enforcement |
| `knowledge/checklists/` | YAML checklist files per client |
| `policy/` | Approval gates and autonomy levels |
| `brain/` | LLM prompts for extraction and communication drafting |

---

## 4. Data Flow

```
1. HTTP POST /onboarding/uc03/validate
       │
2. FastAPI validates request shape (Pydantic)
       │
3. LangGraph: verify_start_conditions
       │    └── BR-01: offer_accepted AND onboarding_form_approved?
       │         ├── NO  → return ONBOARDING_ON_HOLD
       │         └── YES → continue
       │
4. LangGraph: run_validation (DocumentValidationService.validate)
       │    ├── Assert case access (BR-13)
       │    ├── Scan for prompt injection (BR-16)
       │    ├── Load applicable checklist (BR-03)
       │    ├── Check submission status
       │    ├── Validate readability, fields, expiry (BR-05)
       │    ├── Check name consistency (BR-06, BR-07)
       │    ├── Check extraction confidence (BR-07)
       │    ├── Check evidence storage (BR-09)
       │    └── Write audit event (BR-14)
       │
5. LangGraph: conditional routing
       │    ├── injection_detected   → flag_injection node
       │    ├── visa outstanding     → prepare_visa_pack node
       │    ├── HR review needed     → route_to_hr node
       │    └── otherwise            → finalize node
       │
6. Return DocumentValidationResult
```

---

## 5. Business Rules

| Rule | Description | Enforcement |
|---|---|---|
| BR-01 | No processing until offer accepted and form approved | `rules.check_onboarding_start_conditions()` |
| BR-02 | Approved form is name source of truth | `employee_name_form` field used in all comparisons |
| BR-03 | Only request documents in applicable checklist | `checklist.get_requirement()` |
| BR-04 | Reminders contain only outstanding items | `checklist.get_outstanding()` |
| BR-05 | Validate readability, fields, expiry | `rules.check_readability/fields/expiry()` |
| BR-06 | Flag name mismatches for HR | `rules.check_name_consistency()` |
| BR-07 | Never guess low-confidence extractions | confidence < 0.7 → `LOW_CONFIDENCE` finding |
| BR-08 | Family docs only when sponsorship applicable | `applies_to: [sponsorship:true]` in YAML |
| BR-09 | Compliance complete only after evidence stored and approved | `rules.determine_compliance_status()` |
| BR-10 | Prepare visa pack for PRO, never submit | `VisaPackStatus.READY_FOR_PRO` max; SUBMITTED is human-only |
| BR-11 | Approval gates enforced server-side | `hooks.approval.require_approval()` raises if no record |
| BR-12 | Sensitive fields masked | `guardrails.pii.mask_sensitive_label()` |
| BR-13 | Case-level access control | `guardrails.access.assert_case_access()` |
| BR-14 | Full audit trail | `audit_event()` on every action |
| BR-15 | Unrequested docs flagged, not processed | `UNREQUESTED_SENSITIVE` finding |
| BR-16 | Document text is untrusted | `guardrails.injection.scan_for_injection()` |

---

## 6. API Endpoints

### POST `/onboarding/uc03/validate`

Validates a document submission for an onboarding case.

**Request body:** `DocumentValidationRequest`  
**Response:** `DocumentValidationResult`  
**Auth:** Actor ID + type passed in request body; case-level access enforced server-side

**Status codes:**
- `200` — validation completed (findings may include issues)
- `422` — request body failed Pydantic validation
- `403` — access denied or approval required

**Example request:**
```json
{
  "case_id": "case-2024-001",
  "employee_id": "emp-0042",
  "employee_name_form": "Mohammed Al Rashidi",
  "offer_accepted": true,
  "onboarding_form_approved": true,
  "document_type": "passport",
  "submission_status": "RECEIVED",
  "extracted_name": "Mohammed Al Rashidi",
  "expiry_date": "2029-03-15",
  "readable": true,
  "required_fields_present": true,
  "extraction_confidence": 0.96,
  "evidence_stored": false,
  "location": "UAE",
  "requesting_actor_id": "hr-user-007",
  "requesting_actor_type": "HR_USER"
}
```

**Example response:**
```json
{
  "case_id": "case-2024-001",
  "document_type": "passport",
  "document_status": "UNDER_REVIEW",
  "validation_findings": ["VALID"],
  "outstanding_requirements": ["emirates_id", "degree_certificate", "experience_letter"],
  "evidence_status": "NOT_STORED",
  "hr_decision": "PENDING",
  "compliance_item_status": "PENDING_APPROVAL",
  "visa_pack_status": "NOT_APPLICABLE",
  "required_next_action": "HUMAN_CONFIRMATION",
  "approval_status": "PENDING",
  "audit_reference": "evt-uuid-here",
  "detail": "Document type: passport | Status: READY_FOR_HUMAN_CONFIRMATION | Findings: VALID",
  "draft_communication": null,
  "injection_detected": false
}
```

---

### POST `/onboarding/uc03/approval`

HR submits an approval decision for a protected action.

**Query params:** `case_id`, `action_type`, `actor_id`, `actor_type`, `status`, `notes`  
**Response:** `ApprovalRecord`

**Allowed action types:** `SEND_REMINDER`, `SEND_REPLACEMENT_REQUEST`, `WRITE_SYSTEM`,
`COMPLETE_COMPLIANCE`, `PREPARE_VISA_PACK`, `ACCESS_REQUEST`

---

### GET `/onboarding/uc03/audit/{case_id}`

Returns the full audit trail for a case.

**Query params:** `requesting_actor_id`  
**Response:** `list[AuditEvent]`

---

## 7. Data Models

### DocumentValidationRequest — key fields

| Field | Type | Required | Note |
|---|---|---|---|
| case_id | str | Yes | |
| employee_name_form | str | Yes | Source of truth name |
| offer_accepted | bool | Yes | BR-01 |
| onboarding_form_approved | bool | Yes | BR-01 |
| document_type | str | Yes | Must match checklist ID |
| submission_status | SubmissionStatus | Yes | |
| extracted_name | str \| None | No | None = unknown |
| readable | bool \| None | No | None = unknown = flagged |
| extraction_confidence | float [0,1] | No | Default 1.0 |
| requesting_actor_id | str | Yes | BR-13 |

### DocumentValidationResult — key fields

| Field | Type | Note |
|---|---|---|
| validation_findings | list[ValidationFinding] | Always at least one entry |
| compliance_item_status | ComplianceStatus | COMPLETE requires human approval |
| required_next_action | RequiredAction | Single most important action |
| audit_reference | str | UUID linking to audit event |
| injection_detected | bool | Always present |

---

## 8. Checklist Configuration

Checklists live in `knowledge/checklists/{client_id}.yaml`.  
Default checklist: `knowledge/checklists/default.yaml`

Documents can be made conditional using `applies_to`:

```yaml
- id: emirates_id
  applies_to: [location:UAE]      # only UAE employees

- id: family_documents
  applies_to: [sponsorship:true]  # only sponsored employees

- id: visa_documents
  applies_to: [visa_required:true]
```

To add a new client checklist: create `knowledge/checklists/{client_id}.yaml`
and pass `client_id` in the request. No code change required.

---

## 9. Approval Gate Flow

```
Agent produces draft/action
         │
         ▼
hooks.approval.require_approval(case_id, action_type)
         │
    ┌────┴────┐
  APPROVED   NOT APPROVED
    │              │
  Execute      Raise ApprovalRequiredError
  action            │
                 HTTP 403
                 Audit logged
```

All gates are in `policy/approval_gates.yaml`.

---

## 10. LangGraph Workflow

```
START
  │
  ▼
verify_start_conditions ──[on hold]──► on_hold → END
  │
  [proceed]
  ▼
run_validation
  │
  ├──[injection]──────────► flag_injection ──┐
  ├──[visa outstanding]───► prepare_visa_pack─┤
  ├──[HR review needed]───► route_to_hr ──────┤
  └──[clean]─────────────────────────────────┤
                                             ▼
                                          finalize → END
```

Each node updates `UC03State` (TypedDict). State flows through every node — no
shared mutable state between concurrent requests.

---

## 11. Security Considerations

| Threat | Mitigation |
|---|---|
| Prompt injection via document text | `guardrails.injection.scan_for_injection()` before any text is processed |
| Cross-case document access | `guardrails.access.assert_case_access()` on every request |
| Unauthorised actions | `hooks.approval.require_approval()` raises before protected actions |
| Sensitive data in logs | Audit events log document_type and findings only — never raw content |
| Hardcoded secrets | All credentials from Azure Key Vault via `azure-identity` |
| Unrequested sensitive documents | `UNREQUESTED_SENSITIVE` finding + retention policy action |

---

## 12. Limitations and Known Gaps

| Gap | Current state | Resolution |
|---|---|---|
| Document extraction | `MockDocumentExtractor` — returns configurable mock results | Replace with Azure Form Recognizer adapter when credentials available |
| Knowledge retrieval | No Azure AI Search integration yet | Requires `AZURE_SEARCH_ENDPOINT` and `AZURE_SEARCH_API_KEY` |
| Persistence | `InMemoryCaseStore` — data lost on restart | Replace with Cosmos DB or PostgreSQL adapter |
| HRIS integration | Mock MCP server | Requires HRIS base URL and credentials |
| Graph API integration | Mock MCP server | Requires Azure AD app registration |
| Authentication/JWT | Actor ID passed in request body | Add Azure AD JWT middleware |
| Multi-language extraction | Not implemented | Requires multilingual OCR model |
| Attestation tracking | Checklist item exists; attestation-specific logic not built | UC-03 extension |

---

## 13. Local Setup

```bash
# Install dependencies
uv sync --dev

# Copy config template
cp .env.example .env
# Edit .env with your values (leave blank for local mock-only mode)

# Run tests
uv run pytest tests/ -q

# Run evals
uv run python evals/run_evals.py

# Start server
uv run uvicorn src.main:app --reload
# API docs: http://127.0.0.1:8000/docs
```

---

## 14. File Map

```
src/agents/onboarding/uc03/
├── __init__.py         exports key symbols
├── models.py           DocumentValidationRequest, DocumentValidationResult
├── rules.py            BR-01 to BR-16 as pure functions
├── checklist.py        ChecklistService
├── extraction.py       DocumentExtractor protocol + MockDocumentExtractor
├── validator.py        DocumentValidationService (17-step pipeline)
└── workflow.py         LangGraph StateGraph

guardrails/
├── access.py           Case-level access control
├── injection.py        Prompt injection detection
└── pii.py              Sensitive field classification

hooks/
├── audit.py            Pre/post tool-call audit hooks
└── approval.py         require_approval() — server-side gate

knowledge/checklists/
├── default.yaml        10-document default checklist
└── loader.py           YAML loader + context filter

policy/
├── approval_gates.yaml Gates per protected action
└── autonomy.yaml       Agent autonomy levels 1–3

brain/onboarding/
├── uc03_extraction.md      LLM extraction prompt
└── uc03_communication.md   LLM communication drafting prompt

context_layer/skills/
└── uc03_document_validation.yaml  Skill definition for orchestrator (also default.yaml)

specs/
└── uc03_spec.md        This document

evals/
├── datasets/uc03_scenarios.json   26 test scenarios as eval dataset
├── evaluators/uc03_evaluator.py   Evaluator script
└── run_evals.py                   Eval runner

mcp/
├── hris/               HRIS MCP server (mock)
├── graph/              Microsoft Graph MCP server (mock)
├── itsd/               IT Service Desk MCP server (mock)
└── documents/          Document storage MCP server (mock)
```
