# Document Extraction — Implementation

Date: 2026-11-05. The source analysis is in `DOCUMENT_EXTRACTION_SOURCE_ANALYSIS.md`.

HR can now start a new onboarding from the completed onboarding-form document, in the existing **Start New Onboarding**
form. UC01's own extractor reads the document, the fields that map exactly are prefilled, and HR corrects and confirms.
The form then follows the unchanged path: HR approval → UC01 → UC02 → UC03 → UC04 ∥ UC05.

## 1. What was ported, and what was built

**Ported from the source** (UC01 only; the source has no UI and its orchestrator never calls extraction):

- **UC01's `sub_agent/documents` package**, newer than the target's copy:
  - `.docx` and text-based `.pdf` readers;
  - the deterministic 2026-template extraction, including the supplementary rows;
  - validation; budget lines; providers; form-details persistence code; the source-file store.
- **Its wiring:** `api/{app,container,schemas,settings}.py`, `crypto.py`, `adapters/memory.py`, `adapters/supabase/*`.
- **Fixtures, docs and tests**, plus the optional `pdf` extra (`pypdf>=5.1`).
- **Kept from the target:** `llm/gateway_client.py`, which has a fix the source lacks, and its test.

**Built in the target:**

| Layer | What |
|---|---|
| UC01 worker | Operation `extract_onboarding_document`. It calls UC01's `DocumentIntakeService.extract`, `validate_extraction` and `safe_extraction_view`. It is read-only: no case, no audit write, no storage |
| Orchestrator | `orchestration/onboarding_documents.py`: upload checks, the call to UC01, the state mapping, the prefill, actionable errors, and the extraction summary by id |
| API | `POST /v1/orchestration/onboarding-documents/extract`. `StartCaseRequest` gains `source_extraction_id` and `extraction_reviewed`. `CaseView` and `GET …/form-approval` gain `form_extraction`. `/healthz` reports the switch |
| Case state | `form_extraction`: statuses, field names, document digest, the fields HR changed or entered, and the review confirmation. **No extracted values** |
| UI | `OnboardingDocumentIntake` inside the existing `StartOnboardingModal`: upload, status steps, field table, issues, prefill markers and review confirmation. The approval panel and the Intake panel show the recorded source document |

## 2. Data flow

```
HR (Start New Onboarding modal)
  └─ upload .docx / .pdf ──► POST /v1/orchestration/onboarding-documents/extract
        role case_operator_roles, kill switch, enabled, size (streamed), extension, media type, signature
        └─ UC01 worker: extract_onboarding_document  (UC01 code; deterministic; no model; nothing stored)
              read_document → extract_onboarding_form → validate_extraction → safe_extraction_view
        ◄─ REJECTED code | errors, review_reasons, outstanding_documents, safe field view, document digest
     orchestrator: FAILED | VALIDATION_REQUIRED | READY_FOR_REVIEW, the prefill (PRESENT and exact only),
                   the summary kept in process memory under extraction_id (tenant, statuses, names)
  ◄─ field table, missing / invalid / uncertain, review reasons, prefill
HR corrects the form, confirms the review if anything was flagged
  └─ POST /v1/orchestration/cases {form fields, source_extraction_id, extraction_reviewed}
        the server looks up ITS summary (same tenant), enforces the review and the mandatory fields,
        records form_extraction (no values) in the case state
     → onboarding_form_approval gate (hr_lead; separation of duties) ── unchanged
     → UC01 open_onboarding_case from the approved form ── unchanged
     → case store: SQLite / Supabase case_orchestration_state (form_extraction inside orchestrator_context.state)
     → UC02 planning → UC03 collection → UC04 ∥ UC05 ── unchanged
```

## 3. States

| State | Where it comes from |
|---|---|
| `UPLOADED` → `EXTRACTING` | The UI, while the request is in flight. The file travels with the request, so these are one step |
| `EXTRACTION_COMPLETED` | UC01 answered |
| `VALIDATION_REQUIRED` | UC01 reported **document** errors: a mandatory field missing or invalid. Caller-context errors (email, work-location type) are inputs on the form, not document problems |
| `READY_FOR_REVIEW` | No document errors. `review_required` is set when UC01 gave review reasons (uncertain or invalid fields, outstanding documents, example responsibilities, a past joining date) |
| `FAILED` | Refused, with `{code, reason, action}`:<br>• **by the upload checks:** `UNSUPPORTED_FORMAT`, `EMPTY_FILE`, `FILE_TOO_LARGE`, `NOT_A_DOCX`, `NOT_A_PDF`;<br>• **by UC01's reader:** `NOT_A_DOCX`, `CORRUPT_DOCX`, `UNSAFE_XML`, `TOO_MANY_PARTS`, `TOO_LARGE_UNCOMPRESSED`, `CORRUPT_PDF`, `ENCRYPTED_PDF`, `TOO_MANY_PAGES`, `NO_TEXT_LAYER`, `PDF_SUPPORT_UNAVAILABLE`;<br>• `TEMPLATE_NOT_RECOGNISED`;<br>• `EXTRACTION_DISABLED`;<br>• `UC01_UNAVAILABLE` |
| `APPROVED` | The HR lead approved the form (the existing gate). Shown with the source-document summary |

Errors never include a stack trace or an exception message: only the code, a short reason and the action.

## 4. Mapping to UC01's input

| Form field | Extraction field | Rule |
|---|---|---|
| `candidate_name` | `employee.employee_name` | Prefilled only when `PRESENT` |
| `designation` | `employee.designation` | Prefilled only when `PRESENT` |
| `joining_date` | `employee.joining_date` | Prefilled only when `PRESENT`. An `UNCERTAIN` date is not prefilled |
| `candidate_email` | `supplementary.employee_email` | Prefilled only when `PRESENT` |
| `department` | `supplementary.department` | Prefilled only when `PRESENT` |
| `grade` | `supplementary.grade` | Prefilled only when `PRESENT` |
| `work_location_type` | `supplementary.work_location` | Only if the value is exactly `OFFICE` or `CLIENT_SITE`; a place name is not a type |

- **Reference only, never prefilled:**
  - `employee.reporting_to`: a name, not a manager id;
  - `supplementary.work_location`: UC01 never takes the placement from a document;
  - `supplementary.nationality`: not the client's nationality-group code;
  - probation, employment duration, issuing entity.
- **Sensitive** (final salary, mobile, parents' names): status only. The value never leaves UC01's safe view. Salary
  was never carried by the Control Center (UC01 receives `withheld`), and still is not.

## 5. Validation and review rules (UC01's, enforced at submit)

1. **Known extraction.** The `source_extraction_id` must exist and belong to the caller's tenant; otherwise 422. The
   browser cannot supply the provenance itself: `form_extraction` is not a request field, and extra fields are
   refused.
2. **Review confirmation.** When UC01 found document errors or review reasons, `extraction_reviewed` must be `true`.
3. **Mandatory fields entered.** A mandatory field the document lacks (name, designation, joining date) must be
   entered on the form. A missing salary can only be acknowledged; the approver sees it under "Document lacked".
4. **The form's own rules still apply,** and the form still needs the HR lead's approval. Extraction never approves,
   never opens a case, and never writes a record.

## 6. Security

- **Authorization:** extraction needs `case_operator_roles`, checked before the body is read. The kill switch applies.
  The case's tenant is the verified actor's.
- **File checks:** a streamed size cap (at most UC01's 10 MB); an extension allow-list; the media type must match;
  the file signature (`PK\x03\x04` / `%PDF-`) is checked. Only the base name is used.
- **UC01's reader limits** cover zip bombs, part counts, DTD/XXE, encrypted PDFs, page counts and scans.
- **No storage.** The orchestrator never writes the file. UC01's source-file store stays inactive (in-memory UC01).
- **Prompt injection.** No model reads the document on this path. UC01's advisory model is used only in its
  system-key `process` route, not in `extract`. A document's text is data: the injection test proves an
  "approve this form" instruction is refused as `TEMPLATE_NOT_RECOGNISED`, and nothing is approved or opened.
- **Privacy:**
  - the case keeps statuses and field names only;
  - `CaseView` names nobody (who extracted and who confirmed only via `GET …/form-approval`, for identity readers);
  - logs carry the extraction id, status, digest and codes only.
- **Process memory:** the extraction summary, with the prefilled values used to tell what HR changed, is kept for
  24 h, 500 entries at most, like the existing intake inputs. The values are dropped when the case records the
  summary.
- **Credentials:** nothing new reaches the browser.

## 7. Database changes

**None.**

- `form_extraction` is part of the case state. That state is already stored by the SQLite case store, or in Supabase
  `case_orchestration_state.orchestrator_context.state` by the Supabase store.
- No table, column or migration was added.
- UC01's proposed form-details tables (`UC01…/docs/schema/proposed/20261105120000_uc01_form_details_and_budget.sql`)
  are ported as a **proposal** and are not applied (see blockers).

## 8. Configuration (names only)

- **Orchestrator** (`HR-Onboarding-Common/.env.example`):
  - `ONBOARDING_DOCUMENT_EXTRACTION_ENABLED` (default on);
  - `ONBOARDING_DOCUMENT_MAX_BYTES` (can only lower UC01's 10 MB).
- **UC01** (`UC01_Employee_Record_Creation/.env.example`, existing plus one new name): `UC01_DOCUMENT_BUCKET` (new),
  `UC01_SUPABASE_INTERIM_WRITES`, `UC01_FIELD_ENCRYPTION_KEY`, `UC01_FIELD_ENCRYPTION_KEY_ID`,
  `UC01_FIELD_ENCRYPTION_PREVIOUS_KEYS`, `UC01_LLM_ENABLED`, `UC01_SYSTEM_API_KEY`.
- **Dependency:** UC01's optional extra `pdf = ["pypdf>=5.1"]`. It is installed in UC01's `.venv` and in the
  worker environment the server uses (`%LOCALAPPDATA%/hr-onboarding-envs/uc01`). Without it, a PDF is refused as
  `PDF_SUPPORT_UNAVAILABLE`.

## 9. Tests

| Suite | Passed | Failed | Skipped |
|---|---|---|---|
| `HR-Onboarding-Common` (`pytest tests`), including the new file below | 783 | 0 | 1 (opt-in live Supabase test, unchanged) |
| New: `tests/orchestration/test_onboarding_document_extraction.py` | 21 | 0 | 0 |
| `UC01_Employee_Record_Creation` (`pytest tests`): 1000 before the port, plus the ported extraction tests | 1110 | 0 | 16 (pre-existing opt-in / environment skips) |
| Frontend `npm run test:unit` (`labels.test.ts`, including the extraction labels) | 5 | 0 | 0 |
| Frontend `npm run build` (`tsc` + `vite`) | clean | — | — |

**What the new orchestrator tests cover.** They use the real UC01 worker and UC01's synthetic fixtures.

- **Upload:**
  - valid `.docx`;
  - valid `.pdf` (2026 form);
  - unsupported `.txt`;
  - empty;
  - oversized (413, UC01 never called);
  - wrong signature;
  - corrupted `.docx`;
  - corrupted `.pdf`;
  - mismatched media type.
- **Extraction:**
  - complete (`READY_FOR_REVIEW`, exact prefill, salary status only);
  - missing name (`VALIDATION_REQUIRED`; review and name enforced at submit);
  - missing salary and joining date;
  - uncertain joining date (low confidence: not prefilled, review enforced);
  - a foreign document carrying an injected instruction (`TEMPLATE_NOT_RECOGNISED`; nothing opened or approved);
  - extraction switched off;
  - UC01 unavailable (503, no internals).
- **Integration:**
  - extraction never opens a UC01 case;
  - the case waits at the HR form-approval gate;
  - `form_extraction` holds no extracted value, and `CaseView` names nobody;
  - identity readers see who extracted;
  - forged provenance is refused;
  - tenant isolation (another tenant's id is refused);
  - unauthorized role 403, unauthenticated 401.
- **Full chain** with the fully wired orchestrator: document → HR-lead approval → real UC01 record → real UC02
  plan → UC03 collection.
- **Supabase:** no live database is reached by any test. `form_extraction` travels in the case-state JSON that
  the existing Supabase store already writes (no schema change). The opt-in live Supabase test stays skipped.

The UI components have no component-test framework in this project. They are verified by type-check and build.
They were not exercised in a browser: the running backend must be restarted first.

## 10. Remaining blockers

| Blocker | Needs |
|---|---|
| UC01's extracted form details (personal details, benefits, budget) are not persisted: the five proposed UC01 tables are not applied | DBA approval of the proposed SQL; UC01 Supabase mode (`UC01_FIELD_ENCRYPTION_KEY`) |
| The source document file is not kept, so the approver compares against the document out of band | A decision on storing it (UC01's `UC01_DOCUMENT_BUCKET` store, active only with UC01 Supabase interim writes) |
| A backend restart between extraction and submit loses the extraction id (process memory) | HR uploads again (the error says so); durable storage only together with the item above |
| Only the 2026 onboarding-form template is read; scanned PDFs are refused (no OCR) | UC01 design. Other templates need UC01 extraction rules |
| Salary is not carried by the Control Center | Unchanged design. A missing salary is acknowledged and shown to the approver |
| The running backend does not have this code yet | Restart `scripts/serve_orchestrator.py`; `pypdf` is already installed in its UC01 environment |

## 11. Verification

- **No absolute path of the source project** in the target: a repository-wide scan, excluding `.venv`,
  `node_modules`, caches and builds.
- **No secrets** in any ported or changed file: scanned for JWT, `sk-`, key assignments and Supabase project URLs.
  `.env.example` files hold names and empty placeholders only.
- **No temporary files:** probes ran from the session scratchpad. Every file changed since the port began is listed
  in section 1 or in the analysis document.
- **No source-only dependency:** `pypdf` is UC01's declared optional extra, already listed in the source and the
  target `pyproject.toml`.
- **No duplicates:**
  - one onboarding-form extractor (UC01's);
  - one Start New Onboarding page;
  - one case creation path;
  - no second LLM gateway, datastore or table.
- **The source project was not modified:** its working tree is clean.
