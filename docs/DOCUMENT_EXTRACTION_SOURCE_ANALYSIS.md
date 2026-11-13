# Document Extraction — Source Analysis

Written 2026-11-05, read-only. The source project was not modified.

- **Source:** the upstream `enterprise-multiagent-hr-onboarding` checkout (a separate copy outside this
  repository). Paths below are relative to its root; no absolute path of it is used anywhere in the target.
- **Target:** this repository.

## Where the source implementation is

Document Extraction is **entirely inside UC01's backend**, in `UC01_Employee_Record_Creation/src/sub_agent/documents/`,
exposed as `POST /internal/onboarding-documents`.

- **No frontend.** The source has no Start New Onboarding screen, no upload control, no client state (checked
  across the working tree and every `origin/*` branch).
- **Caller:** a system sends the file as base64 JSON with an `X-System-Key`.
- **Not an LLM extractor.** Extraction is **deterministic** and template-specific: the 2026 onboarding form,
  `TEMPLATE_ID = "onboarding-form-2026"`. Rows are matched by label; text the template prints itself is set aside.
- **Model use is advisory and escalate-only.** One FAST-tier model call checks whether the responsibilities match the
  designation. It is off unless `UC01_LLM_ENABLED`, input-guarded, can only add a review reason, and never extracts
  or approves.

**The target already has an older copy of the same package** (`UC01_Employee_Record_Creation/src/sub_agent/documents/`,
4 Nov). The source is strictly newer (5 Nov). File by file, the differences are almost all source-only additions.

**One exception:** the target's `llm/gateway_client.py` has a fix the source lacks (the gateway is built from the same
environment the LLM mode was decided from). It is kept.

## Component table

| Component | Source file | Purpose | Target equivalent | Action |
|---|---|---|---|---|
| Endpoint | `UC01…/src/sub_agent/api/app.py` `/internal/onboarding-documents` | Base64 JSON → `DocumentIntakeService.process` → case | Same route, older (docx only; no `source_document`) | **needs adaptation**: port the route changes. The Control Center gets its own orchestrator route (below), not this system-key route |
| Request / response | `api/schemas.py` `OnboardingDocumentBody`, `OnboardingDocumentResponse` | Contract (10 MB base64 cap) | Older | **reusable as-is** (port) |
| Service | `documents/service.py` `DocumentIntakeService` (`extract`, `process`, `_process_extraction`, `_form`, `_second_opinion`, `_persist_and_verify`, `safe_extraction_view`) | Read → extract → validate → model advice → review gate → `open_onboarding_case` → persist | Older (no store, persistence, budget, supplementary) | **reusable as-is** (port). The orchestrator uses `extract` + `validate_extraction` + `safe_extraction_view` only; case creation stays on the orchestrator's gated path |
| DOCX reader | `documents/docx_reader.py` | Stdlib zip+XML; size, part and uncompressed caps; DTD refused | Identical | **target equivalent already exists** |
| PDF reader | `documents/pdf_reader.py` | Text PDF via optional `pypdf`, layout rows; max 20 pages; encrypted / no-text-layer refused | — | **missing in target**: port; `pypdf>=5.1` is UC01's optional `pdf` extra |
| Extraction | `documents/extraction.py` | Deterministic template extraction; `SUPPLEMENTARY_SPECS` (email, department, nationality, work location, grade, duration) | Older (no supplementary rows) | **reusable as-is** (port) |
| Schema | `documents/models.py` | `ExtractedField{status,value,source,note}`, sections, `SecretStr` for salary / mobile / parents, metadata (`completeness`, missing / invalid / uncertain) | Older | **reusable as-is** (port) |
| Validation | `documents/validation.py` | Mandatory errors (`employee_name`, `designation`, `joining_date`, `final_salary`, template, caller context) vs review reasons | Older | **reusable as-is** (port) |
| Budget | `documents/budget.py` | Onboarding budget lines with provenance | — | **missing in target**: port (part of the extraction result) |
| Providers | `documents/providers.py` | Docx/PDF provider + synthetic mock provider (refuses non-synthetic fixtures) | Older | **reusable as-is** (port) |
| Form-details persistence | `documents/persistence.py` + `docs/schema/proposed/20261105120000_uc01_form_details_and_budget.sql` | Five UC01 tables. **Proposed, not applied** | — | **needs adaptation**: port the code (in-memory in dev); the tables need DBA approval (not applied) |
| Source-file store | `documents/supabase_store.py`, `adapters/supabase/storage_paths.py`, `client.py` | Stores the file only with `UC01_SUPABASE_INTERIM_WRITES` + `UC01_DOCUMENT_BUCKET` | — | **needs adaptation**: port; inactive in this deployment (UC01 runs in memory) |
| Container / settings / crypto | `api/container.py`, `api/settings.py`, `crypto.py`, `adapters/memory.py`, `adapters/supabase/{repositories,unsupported}.py` | Wiring; key versioning for encrypted personal details | Older | **reusable as-is** (port) |
| LLM gateway client | `llm/gateway_client.py` | Online model via `hr_onboarding_common.llm` | **Target is newer** (environment fix) | **target equivalent already exists**: keep the target's |
| Fixtures | `data/mock/documents/SYNTHETIC_*.docx/.pdf` (5 extra) | Synthetic forms | Partly | **missing in target**: port |
| Tests | `tests/test_document_intake.py` (38), `test_document_pdf.py` (11), `test_form_details_persistence.py` (27), `test_onboarding_budget.py` (20), `test_synthetic_onboarding_form.py` (15), `test_crypto_key_versioning.py`, `test_supabase_adapters.py`, `test_app_startup.py` | Extraction, safety, validation, persistence | Older `test_document_intake.py` | **reusable as-is** (port); keep the target's `test_llm_gateway.py` (it pins the target's gateway fix) |
| Docs | `docs/document-extraction.md`, `docs/uc01-budget-and-persistence.md` | Design | Older | port |
| Isolated E2E runner, seed-counter migration, UC02 handoff tests, `output/` | various | Not document extraction | — | **obsolete / not required** here |
| UC03 extraction (PyMuPDF / OCR / Gemini) | `UC03_Document_Validation/src/sub_agent/extraction.py` | Identity-document validation, not onboarding prefill | Target has it (used by the orchestrator's document upload) | **target equivalent already exists**: unrelated, unchanged |
| Start New Onboarding UI | — (none in source) | — | `onboarding-ui-prototype/src/components/StartOnboardingModal.tsx` (manual entry) | **missing in target**: build the upload, review and prefill in the existing modal |
| Orchestrator integration | — (none in source: the source's orchestrator never calls the document endpoint) | — | `orchestration/api.py`, `orchestrator_agent.py`, UC01 worker | **missing in target**: build |

## Extracted fields (source schema, unchanged)

| Section | Fields |
|---|---|
| employee | `employee_name`, `reporting_to`, `designation`, `joining_date` (date), `probation_months` (3\|6), `final_salary` (secret) |
| offer | `issuing_entity`, `other_promises` |
| employment | `visa_duration_years`, `visa_location_category` (INSIDE_COUNTRY\|OUTSIDE_COUNTRY), `visit_visa_entry_permit`, `direct_entry_permit` |
| documents | `passport`, `passport_cover`, `visa_copy`, `cancellation_paper`, `emirates_id`, `signed_offer_letter`, `photograph`, `degree_certificate` (RECEIVED\|PENDING) |
| personal | `father_name`, `mother_name`, `mobile_number` (all secret), `marital_status` |
| insurance | `plan`, `employee_insurance`, `family_insurance` |
| benefits | `annual_employee_ticket`, `annual_family_ticket`, `hotel_stay_days`, `hotel_budget_and_location` |
| roles | `responsibilities[]`, `is_example`, `summary` |
| supplementary | `employee_email`, `department`, `nationality`, `work_location`, `grade`, `employment_duration` |
| other | `reference_charges[]`, `budget`, `metadata` (sha256, size, template, `completeness` 0..1, missing / invalid / uncertain fields) |

**Status per field:** `PRESENT` / `MISSING` / `INVALID` / `UNCERTAIN`. There is no per-field numeric confidence. The
status and `completeness` play that role, so no confidence is invented.

## Configuration (names only)

- **UC01:**
  - `UC01_LLM_ENABLED`, `LLM_MODE`;
  - `UC01_DOCUMENT_BUCKET` (new);
  - `UC01_SUPABASE_INTERIM_WRITES`, `UC01_FIELD_ENCRYPTION_KEY`, `UC01_FIELD_ENCRYPTION_KEY_ID`,
    `UC01_FIELD_ENCRYPTION_PREVIOUS_KEYS`;
  - `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`;
  - `UC01_SYSTEM_API_KEY`.
- **Gateway:** `LLM_MODEL_FAST` and the related settings.
- **Dependency:** optional `pypdf>=5.1`.

## Integration decision for the target

The source never connected extraction to an orchestrator or a UI. In the target:

1. **Port** the newer UC01 `documents` package and its dependencies, keeping the target's gateway fix.
2. **Extraction only through UC01.** Add a UC01 worker operation that only extracts and validates, using UC01's own
   code. It never opens a case.
3. **Orchestrator route.** It enforces the role, the tenant, the size and the type; returns the safe view (no secret
   values) plus a prefill of the Start New Onboarding fields that map exactly; and records an extraction summary by id.
4. **The existing Start New Onboarding modal** gains upload, extraction status, the field table, missing / invalid /
   uncertain fields, review reasons and prefill. HR corrects, confirms the review, and submits through the existing
   `POST /cases`.
5. **The unchanged path:** form-approval gate (HR lead) → UC01 `open_onboarding_case` → UC02 → … The case keeps the
   extraction summary (statuses and field names, never values) in its state.
