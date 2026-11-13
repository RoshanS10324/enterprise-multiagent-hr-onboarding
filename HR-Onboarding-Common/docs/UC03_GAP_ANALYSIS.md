# UC03 Document Validation — Gap Analysis (standalone + orchestrator integration)

Audit date: 2026-10-29. Method: read-only source review plus test runs and stdin-only Python probes (no files created, `PYTHONDONTWRITEBYTECODE=1`, no external calls; no LLM keys present in the environment, `HR_AGENT_ENV` unset, so the mock extractor was the default). Existing docs (Common `docs/*.md`, workspace `HR_ONBOARDING_ORCHESTRATION_AGENT_ANALYSIS.md`, UC03 `README`/`docs`/`specs`) were treated as claims to check, not as evidence. Paths: `UC03/` = `UC03_Document_Validation/`, `orch/` = `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/`, `otests/` = `HR-Onboarding-Common/tests/orchestration/`.

Out of scope, and not counted as gaps: NDA/policy signature tracking, and employees signing and returning documents. F08 is the opposite problem: those documents currently block the orchestrated stage.

---

## 1. Summary

| Area | Verdict |
|---|---|
| Standalone rule engine (checklist resolution, expiry/readability/fields/confidence/name/injection rules, status mapping, audit events) | Implemented and unit-tested. It works on flags the caller supplies, not on document content. |
| Extraction | Real local PyMuPDF and regex extractor (works on `test_passport.pdf`), plus Gemini fallback. Only the `/upload-and-validate` endpoint calls it. `DocumentValidationService.validate` never calls its extractor (DISCONNECTED). |
| MCP, connector, hook and context-layer scaffolding | Defined only, never invoked. All MCP clients are mock-only. |
| Security | Access control is a no-op (P0). There is no tenant isolation (P0). The approval and audit endpoints are unauthenticated, and HR approval is asserted by the caller (P1). |
| Correction and revalidation | Broken. A corrected resubmission is always flagged `DUPLICATE_REQUEST` and validation is skipped (P1). |
| Persistence | In-memory only, in UC03 itself and in the orchestrator's cached UC03 runtime. |
| Orchestrator integration | The real UC03 can never let `validate_documents` reach COMPLETE: no input path for documents, `ACCEPTED` is never emitted, HR approval is never passed, out-of-scope NDA docs are required, and a UAE Emirates ID is flagged for retention. Every test that passes this stage uses a double (`World._docs_double`) that emits states the real UC03 cannot produce. |

Severity count: P0 × 2, P1 × 5, P2 × 9, P3 × 3 (19 findings, §8).

## 2. Code map

| Concern | Location | Executed on which path |
|---|---|---|
| HTTP API | `UC03/src/main.py`, `UC03/src/api/uc03.py` (`/upload-and-validate`, `/validate`, `/approval`, `/audit/{case_id}`) | UC03 standalone only |
| Validation pipeline (17 steps) | `UC03/src/sub_agent/validator.py::DocumentValidationService.validate` | API, UC03 graph, orchestrator |
| LangGraph wrapper | `UC03/src/sub_agent/workflow.py::build_uc03_graph` (verify_start_conditions → run_validation → flag_injection / prepare_visa_pack / route_to_hr → finalize) | Orchestrator, UC03 tests |
| Rules | `UC03/src/sub_agent/rules.py` | All paths |
| Name match (LLM-assisted) | `UC03/src/sub_agent/name_agent.py` (exact match, else Gemini, else Claude, else NAME_MISMATCH) | All paths (the LLM only runs when keys are set) |
| Extraction | `UC03/src/sub_agent/extraction.py` (Mock / Local / LLM / Hybrid, `get_extractor`) | `/upload-and-validate` only |
| Checklist | `UC03/knowledge/checklists/default.yaml`, `loader.py`, `src/sub_agent/checklist.py` | All paths |
| Comms drafts | `UC03/src/sub_agent/comms.py` (template, or Claude when `ANTHROPIC_API_KEY` is set). Drafts are never sent. | All paths |
| Stores | `UC03/src/state/store.py` (`CaseStore`/`AuditStore` Protocols plus in-memory implementations, process singletons) | All paths |
| Guardrails | `UC03/guardrails/access.py`, `injection.py` (used); `pii.py`, `policy.yaml` (unused) | — |
| Scaffolding | `UC03/mcp/*`, `connectors/outlook/*`, `hooks/*`, `context_layer/*`, `policy/*.yaml`, `src/sub_agent/agent.py`, `fakes.py`, `src/orchestrator/` (empty) | Not executed (§4) |
| Orchestrator adapter | `orch/adapters/uc03_documents.py` (`_ensure_uc03_importable`, `_build_uc03_runtime`, `_ensure_case_registered`, `_validate_all`, `call_uc03_validate_documents`) | Orchestrator |
| Graph nodes | `orch/orchestrator_agent.py`: `validate_documents` (:343, wrapped by `_stage_node` :271-319), `route_after_documents` (:101), `await_document_correction` (:113, `_await_node` :125-134) | Orchestrator |
| Tests | `UC03/tests/{unit,integration}`; `otests/test_uc03_adapter.py`; UC03 double in `otests/_lifecycle.py:165-179` | — |

**Sibling-package importability.** UC03's wheel packages only `src` (`UC03/pyproject.toml:54`). `src` imports `guardrails.*` (`validator.py:5-6`, `extraction.py:11`, `api/uc03.py:10`), `hooks.approval` (`api/uc03.py:11`) and `knowledge.checklists.loader` (`checklist.py:3`, `rules.py:6`). The default YAML also sits outside the wheel.
- UC03's own tests work because pytest adds `pythonpath = ["."]` (`UC03/pyproject.toml:59`) and the run starts from the UC03 root.
- The orchestrator inserts the UC03 repo root at `sys.path[0]` (`uc03_documents.py:33-36`) and runs UC03 inside the Common interpreter. That interpreter needs langgraph, pydantic and pyyaml, all present.
- The root comes from `OrchestratorDeps.uc03_repo_root` (`orch/deps.py:20`). The scheduler CLI defaults it to `"."` when it is not set (`joining_day_scheduler.py:326,340`).
- Common's `pyproject.toml:33` points to `orchestration/settings.py`, which does not exist.

## 3. Standalone functionality

| # | Capability | Status | Evidence |
|---|---|---|---|
| 1 | Intake and required-document identification | PARTIAL | `applies_to` resolution works and is tested (`loader.py:38-59`; `test_uc03_checklist.py` 12 tests). `required: false` (`default.yaml:97,108`) and `min_count: 3` (`:68`) are ignored. `outstanding_requirements` is always the full list: `get_outstanding(ctx, already_complete=set())` (`validator.py:277`); probe P5 shows passport still outstanding after validation. |
| 2 | File metadata and document-type validation | PARTIAL | MIME allowlist and empty-file check exist (`api/uc03.py:56-65`). There is no size limit and no magic-byte check. `document_type` is whatever the caller declares: `WRONG_TYPE` is never emitted (grep shows it only in `rules.py`/`validator.py` consumers). The upload endpoint has no tests. |
| 3 | Extraction and normalization | PARTIAL / DISCONNECTED | Regex fields exist for 4 types (`extraction.py:16-37`); local run gives `confidence 0.878` on `test_passport.pdf`. Other types return `{}`. Extracted field names (`date_of_birth`) do not match the checklist's required fields (`full_name`, `photo_page`, `default.yaml:21`). Expiry parsing assumes DD/MM (`api/uc03.py:82-94`). Name normalization is strip and lowercase only. The validator never calls the extractor (`validator.py:52`, stored and unused). |
| 4 | Validation rules and confidence thresholds | PARTIAL | Rules are implemented and tested (`rules.py:40-113`, `test_uc03_rules.py`). The 0.7 threshold is hard-coded in 4 places (`rules.py:17`, `extraction.py:14`, `validator.py:210`, `name_agent.py:315`). The required-fields check is a caller boolean; the checklist field lists are never compared with `extracted_fields` (`validator.py:183-186`; probe P8: `required_fields_present=True, extracted_fields={} -> ['VALID']`). |
| 5 | Missing / invalid / unreadable / inconsistent handling | PARTIAL | MISSING leads to SEND_REMINDER plus a draft. Expired or unreadable leads to REJECTED plus a replacement draft. Name mismatch or low confidence leads to HR_REVIEW. All are tested (`test_uc03_workflow_e2e.py` 155-318). There are no cross-document consistency checks (DOB or name across docs). `UNSUPPORTED_LANGUAGE` is never emitted, and the extractor's `supported_language`/`injection_suspected` values are dropped (`api/uc03.py:96-122`). |
| 6 | Human review and correction workflow | INCORRECT | Outputs `required_next_action` only. There is no review queue and no correction endpoint. `/approval` is unauthenticated (`api/uc03.py:165-200`). Compliance reads the caller's `hr_review_status`, not the ApprovalRecord (`validator.py:256-263`); probe P3: `hr_review_status=APPROVED`, no record, gives `COMPLETE`. `hooks.approval.require_approval` is never called. |
| 7 | Resume or revalidation after correction | INCORRECT | `received_documents` is updated whatever the outcome (`validator.py:316-324`), so the next submission of the same type gets `DUPLICATE_REQUEST` and the rules are skipped (`:159-175`). Probe P2: `corrected resubmission -> RECEIVED ['DUPLICATE_REQUEST'] HR_REVIEW BLOCKED`. Tests assert this behaviour (`test_uc03_validator.py:195-201`). |
| 8 | Tenant and employee association | INCORRECT | Cases are keyed by `case_id` only (`store.py:23-35`). `client_id` and `employee_id` are stored on first create (`validator.py:60-75`) and never compared again. Access check is a no-op (`validator.py:81`). Probes P1 and P7: an intruder and another tenant were both processed on someone else's case. |
| 9 | Result persistence and auditability | PARTIAL | Audit events (type, findings, statuses, correlation id, no raw content) are written per validation (`validator.py:326-341`). Storage is in-memory only (`store.py:23-80`). `GET /audit/{case_id}` ignores `requesting_actor_id` (`api/uc03.py:203-209`). The access-denied result returns a random `audit_reference` that matches no event (`validator.py:106`). |
| 10 | Mocked extraction vs implemented logic | See §4 | Real: rules, checklist, status mapping, regex injection scan, local PDF extraction. Mocked or optional: `MockDocumentExtractor` (default in local/test/ci, `extraction.py:309-317`), Gemini/Claude name and extraction LLMs, all MCP clients, the Outlook connector. |
| 11 | Tests for valid / invalid / incomplete / ambiguous | PARTIAL | Valid: `TestHappyPath`, scenario_01. Invalid: expired, unreadable, injection. Incomplete: missing doc or fields, evidence. Ambiguous: father-name variant with a fake LLM, no-LLM gives NAME_MISMATCH, confidence boundary. Missing: access denial via `validate`, tenant isolation, revalidation after correction, `/upload-and-validate`, approval-driven compliance. `test_audit_no_raw_content` can never fail (`test_uc03_validator.py:296`: `... or "findings" in detail_str`). |

## 4. Extraction and MCP/tool integration status

| Component | Kind | Invoked by the UC03 execution path? | Evidence |
|---|---|---|---|
| `MockDocumentExtractor` | Mock | Selected by `get_extractor()` when `HR_AGENT_ENV` is local/test/ci. The orchestrator hard-codes it (`uc03_documents.py:46,52`), but `validate()` never calls any extractor. | `extraction.py:61-77,309-317`; `validator.py:52` |
| `LocalDocumentExtractor` (PyMuPDF, pytesseract OCR fallback) | Real, local | Only via `/upload-and-validate` → `HybridDocumentExtractor` when `HR_AGENT_ENV` is not local/test/ci | `extraction.py:82-175`; `api/uc03.py:73-77` |
| `LLMDocumentExtractor` (Gemini) | External; mocked in tests through an injected client | Hybrid fallback when local confidence is below 0.7 | `extraction.py:189-304` |
| Name-match LLM (Gemini, then Claude) | External, optional | `validate()` step 7 when names differ and a key is set; otherwise conservative NAME_MISMATCH | `name_agent.py:338-364` |
| Comms LLM (Claude) | External, optional | Drafts when SEND_REMINDER or SEND_REPLACEMENT is required; nothing is ever sent | `comms.py:35-48` |
| `mcp/base.py` and `mcp/{documents,graph,hris,itsd}/client.py` | Mock-only (`_real_call` raises NotImplementedError) | **No.** Only imported by other `mcp/*` modules (grep over `src`, `tests`, workspace). `DocumentStorageClient` (the natural evidence store) is unused; its mock metadata is hard-coded `case-mock`. | `mcp/base.py:32-44`; `mcp/documents/client.py:110-151` |
| `connectors/outlook/{base,mock}.py` | Mock | No | Not imported by UC03 `src` |
| `hooks/approval.require_approval`, `hooks/audit.before/after_tool_call` | Real helpers | No. Only `ApprovalRequiredError` is imported, for an except clause that can never fire (`api/uc03.py:11,135`). | grep |
| `context_layer/uc03_context.py::UC03ContextAssembler`, `context_layer/skills/*.yaml` | Real code / config | No | grep: no callers |
| `guardrails/pii.py`, `guardrails/policy.yaml`, `policy/{approval_gates,autonomy}.yaml` | Config / helpers | No (never loaded) | grep |
| `src/sub_agent/agent.py::SubAgent.run`, `fakes.py::FakeSubAgent` | Stub / double | No (`run` raises NotImplementedError) | `agent.py:23-24` |

Spec claims such as "integrates four MCP clients" and "`require_approval()` raises before protected actions" (`UC03/specs/uc03_spec.md:19,320`) are false.

## 5. Correction and resume

| Step | Standalone UC03 | Orchestrated |
|---|---|---|
| Submitting a corrected document | `/validate` again with the same `case_id` and `document_type`. The result is DUPLICATE_REQUEST, rules are skipped, HR_REVIEW, BLOCKED (probe P2). No supersede or reset API exists. | No route or field carries documents: `StartCaseRequest` uses `extra="forbid"` and has no documents field (`orch/api.py:54-57`), `OnboardingCaseInput` has none (`orch/contracts.py:20-63`), and `initial_state` never sets `intake_trigger.documents` (`orch/orchestrator_agent.py:386-390`). |
| Resume | n/a | `resume_case` (`:558-567`) re-runs the graph. Completed stages are skipped (`_stage_node` :278-279), `clear_stage_items` drops old UC03 blockers (`_shared.py:66-71`), and **all** applicable docs are re-validated with the same (empty) input. |
| Same-process re-run | Second validation of a RECEIVED type gives DUPLICATE_REQUEST | Probe B2: passport `UNDER_REVIEW` becomes `RECEIVED / BLOCKED` on retry |
| After restart | All state is lost | The cached UC03 stores on `deps` are lost (`deps.py:46-49`). Probe B3: a fresh runtime returns `UNDER_REVIEW` again, so behaviour differs from B2. Only the 4-field projection survives in the CaseStateStore. |
| Proving test | The tests codify the DUPLICATE behaviour (`test_uc03_validator.py:195`) | `test_uc05_lifecycle.py:162-174` proves resume with the **double** only (`docs_valid` toggled). No real-UC03 correction test exists. |

## 6. Integration contract with the orchestrator

How the adapter calls UC03: synchronously wrapped async in-process. `call_uc03_validate_documents` runs `asyncio.run(_validate_all(...))` (`uc03_documents.py:124`). That builds one cached UC03 graph per `OrchestratorDeps` with fresh `InMemoryCaseStore`/`InMemoryAuditStore` (`:39-58`), registers the case and adds the invoking actor to `authorized_actor_ids` (`:61-79`), then calls `graph.ainvoke` once per applicable checklist document (`:93-118`).

**Request mapping (`DocumentValidationRequest`, `uc03_documents.py:100-111`)**

| UC03 field | Source | Issue |
|---|---|---|
| `case_id`, `client_id` | `state.case_id` (UC01 uuid4), `state.client_id` | `client_id` is never checked by UC03 (F02) |
| `employee_id` | `state.employee_id` (UC01 `employee_no`, not `employee_ref`) | `None` raises a pydantic error and the stage FAILs (probe B5, F15) |
| `employee_name_form` | `intake_trigger.candidate_name` | `None` makes the stage FAIL (probe B6) |
| `offer_accepted`, `onboarding_form_approved` | Hard-coded `True` | Relies on routing order |
| `submission_status`, `extracted_name`, `expiry_date`, `readable`, `required_fields_present`, `evidence_stored`, `document_ref` | `intake_trigger.documents[doc]` | Never populated in production (F06) |
| `requesting_actor_id` / `_type` | `actor.on_behalf_of` / `ActorType.SYSTEM` | A human HR id is recorded as SYSTEM in the UC03 audit |
| `correlation_id` | `state.correlation_id` | OK |
| **Not passed** | `role`, `grade`, `nationality`, `location`, `visa_required`, `sponsorship_applicable`, `hr_review_status`, `attestation_confirmed`, `extraction_confidence`, `extracted_fields` | The validator rebuilds the checklist context without location (F07). Compliance can never be COMPLETE (F05). NDA/policy docs always fail (F08). |

**Output normalization (`:131-162`)**

| Orchestrator field | Value | Issue |
|---|---|---|
| `document_validation_result[doc]` | `{document_status, compliance_item_status, hr_decision, evidence_status}` | Findings and next action are dropped (only in the free-text blocker reason). Not exposed in `CaseView` (`orch/api.py` `case_view`). |
| `evidence_references` | `r["document_ref"]` | `DocumentValidationResult` has no `document_ref` (`UC03/src/sub_agent/models.py:75-91`), so it is always `[]` (probe B1) |
| Stage status | COMPLETE only if every doc is `ACCEPTED` **and** `COMPLETE` (`:29-30,147-153`), otherwise BLOCKED | UC03 never assigns `ACCEPTED` (only `enums.py:20`; `validator.py:238-253`). Unreachable (F05). |
| `blockers` | One per doc, `code=str(document_status)`, `owner=hr_admin` | Codes are raw UC03 statuses (`UNDER_REVIEW`, `RECEIVED`). `stage_errors` is never set for this stage. |
| `outstanding_tasks` | Union with the doc types (`:155`) | Never removed on a later COMPLETE (latent) |
| `pending_approvals` | None added | The doc claim "hr_decision surfaced into pending_approvals" (workspace analysis `:483`) is false |

**Blocking downstream.** Correct mechanically: BLOCKED routes to `await_document_correction` and then END (`orchestrator_agent.py:101,113,370-371`). UC04 never ran in the double-based test (`test_uc05_lifecycle.py:165-167`).

**Test matrix (real vs double)**

| Test | UC03 used | What it exercises |
|---|---|---|
| `otests/test_uc03_adapter.py` (3 tests) | **Real** UC03 graph, direct adapter call (not through the graph) | Missing docs give BLOCKED, and the projection has no raw fields. It uses synthetic `documents` and `grade`/`nationality` keys that production never sets. Test 2 is effectively vacuous. COMPLETE is never reached. |
| `otests/test_orchestrator_e2e.py` (`orchestrator` fixture, real wiring) | Real UC03 wired but **never executed** | Test 1 stops at PLAN_TASKS FAILED (:49). The day-one test marks VALIDATE_DOCUMENTS COMPLETE by hand (:147), so `_stage_node` skips it. The docstring names a nonexistent `_force_uc02_complete`. |
| Every `World`-based test (`test_uc05_lifecycle`, `test_approval_authorization`, `test_employee_identity`, `test_joining_day_scheduler`, `test_orchestrator_api`, `test_rsvp_and_sensitive_data`, `test_welcome_recovery`) | **Double** `World._docs_double` (`_lifecycle.py:79,146,165-179`) | It emits `ACCEPTED`/`COMPLETE`, which the real UC03 cannot produce. Blocking and resume are proven only against this double. |
| `otests/test_routing.py:41,45` | None (pure routing function) | Route COMPLETE leads to equipment_access, BLOCKED leads to await_document_correction |

## 7. Tests

| Command (PowerShell) | Verbatim result |
|---|---|
| `$env:PYTHONDONTWRITEBYTECODE="1"; Set-Location "C:\Studies\Projects\HR Onboarding Agent\UC03_Document_Validation"; & "C:\Users\ajaym\AppData\Local\Temp\claude\c--Studies-Projects-HR-Onboarding-Agent\4c74be37-a994-4d47-bd20-bc79e0c23931\scratchpad\venvs\uc03\Scripts\python.exe" -m pytest -q -p no:cacheprovider tests` | `200 passed, 1 warning in 5.65s` (the warning is a PyMuPDF `fitz` FutureWarning in `test_extract_returns_low_confidence_on_missing_file`) |
| `$env:PYTHONDONTWRITEBYTECODE="1"; Set-Location "C:\Studies\Projects\HR Onboarding Agent\HR-Onboarding-Common"; .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/orchestration/test_uc03_adapter.py` | `3 passed in 0.82s` |

Environment: UC03 venv Python 3.12.6, langgraph 1.2.15, pydantic 2.14.1. Common venv Python 3.12.6, langgraph 1.2.18. `UC03/evals` was not run; the given command covers `tests` only.

**What they prove.** Each rule and status mapping behaves as coded on caller-supplied flags; the checklist resolves by context; local regex extraction works on text PDFs; the injection regex works; the graph routes as coded; the adapter blocks when docs are missing.

**What they don't prove.** Access control or tenant isolation; revalidation after correction; the upload endpoint; approval-driven compliance; any orchestrated COMPLETE with the real UC03; restart or idempotency.

**Ad-hoc probes** (stdin Python, no files). Run from `UC03/` with the UC03 venv (P1–P8), and from `HR-Onboarding-Common/` with its venv (B1–B6). Verbatim output lines:

```
P1 intruder on case C-X -> HUMAN_CONFIRMATION UNDER_REVIEW authorized_ids: {'hr-A'}
P2 expired -> REJECTED ['EXPIRED_DOCUMENT'] SEND_REPLACEMENT_REQUEST
P2 corrected resubmission -> RECEIVED ['DUPLICATE_REQUEST'] HR_REVIEW BLOCKED
P3 self-asserted APPROVED, no approval record -> UNDER_REVIEW COMPLETE approval_status: PENDING next: NONE
P4 best-case default -> UNDER_REVIEW PENDING_APPROVAL HUMAN_CONFIRMATION evidence: STORED (evidence_stored=False in request)
P5 outstanding after passport validated: True 7
P6 audit events case C-X: ['DOCUMENT_VALIDATED', 'DOCUMENT_VALIDATED', 'DOCUMENT_VALIDATED', 'DOCUMENT_VALIDATED']
P7 other tenant same case_id -> ['DUPLICATE_REQUEST'] HR_REVIEW case.client_id: default
P8 required_fields_present=True, extracted_fields={} -> ['VALID']
local passport: {... 'extracted_fields': {'passport_number': 'A1234567', 'nationality': 'British', 'expiry_date': '01/03/2030', 'date_of_birth': '15/06/1985'}, 'confidence': 0.878, ...}
B1 all-valid docs -> stage: blocked   (signed_offer_letter/passport/degree/experience/payslips UNDER_REVIEW PENDING_APPROVAL; emirates_id, nda, policy_acknowledgements RECEIVED BLOCKED)
B1 evidence_references: [] | blocker codes: ['RECEIVED', 'UNDER_REVIEW']
B2 retry same deps -> passport: {'document_status': 'RECEIVED', 'compliance_item_status': 'BLOCKED', 'hr_decision': 'PENDING', 'evidence_status': 'STORED'}
B2 UC03 audit events for C1 after 2 runs: 16
B3 'restart' (fresh deps) -> passport: UNDER_REVIEW | UC03 audit for C1: 8
B4 location=Dubai -> doc types checked: ['degree_certificate', 'experience_letter', 'nda', 'passport', 'payslips', 'policy_acknowledgements', 'signed_offer_letter']
B5 employee_id None -> failed INTERNAL_ERROR 1 validation error for OnboardingCase
B6 candidate_name None -> failed INTERNAL_ERROR 1 validation error for OnboardingCase
emirates_id ['UNREQUESTED_SENSITIVE'] APPLY_RETENTION_POLICY ; nda ['MISSING_FIELDS'] SEND_REPLACEMENT_REQUEST
top-level 'mcp' resolves to: ...UC03_Document_Validation\mcp\__init__.py
```

## 8. Findings register

| ID | Status | Sev | Title | Owner | Common-fixable |
|---|---|---|---|---|---|
| UC03-F01 | INCORRECT | P0 | Case access control is a no-op | UC03 | no (compensate with `require_role`) |
| UC03-F02 | INCORRECT | P0 | No tenant isolation in UC03 stores | UC03 | partial (namespace case ids in the adapter) |
| UC03-F03 | INCORRECT | P1 | Unauthenticated approval/audit API; HR approval asserted by the caller | UC03 | no |
| UC03-F04 | INCORRECT | P1 | Revalidation after correction impossible (DUPLICATE_REQUEST) | UC03 | no |
| UC03-F05 | INCORRECT | P1 | Real UC03 can never let `validate_documents` COMPLETE | Both | partial |
| UC03-F06 | MISSING | P1 | No orchestrator path to submit or correct documents | Common | yes |
| UC03-F07 | INCORRECT | P1 | Checklist context dropped; UAE Emirates ID flagged APPLY_RETENTION_POLICY | Common | yes |
| UC03-F08 | INCORRECT | P2 | Out-of-scope NDA/policy-ack docs block the orchestrated stage | Common (+UC03) | yes |
| UC03-F09 | DISCONNECTED | P2 | Extraction not used by `validate()`, the graph or the orchestrator | UC03 | no |
| UC03-F10 | INCORRECT | P2 | Upload-path validation is vacuous or fabricated | UC03 | no |
| UC03-F11 | INCORRECT | P2 | Outstanding / required / evidence semantics wrong | UC03 | no |
| UC03-F12 | PARTIAL | P2 | In-memory only; orchestrated UC03 state lost on restart | Both | yes |
| UC03-F13 | INCORRECT | P2 | Retries not idempotent (audit dupes, outcome flips) | Both | partial |
| UC03-F14 | PARTIAL | P2 | Result normalization gaps (evidence refs, approvals, codes) | Common | yes |
| UC03-F15 | INCORRECT | P2 | Missing identity fields make the stage FAIL instead of BLOCK | Common | yes |
| UC03-F16 | PARTIAL | P2 | Test gaps; the double emits states UC03 cannot produce | Both | yes (common tests) |
| UC03-F17 | DISCONNECTED | P3 | MCP/connector/hook/context scaffolding defined only | UC03 | no |
| UC03-F18 | UNVERIFIED | P3 | In-process bridge risks (`sys.path` shadowing, `asyncio.run`/Lock) | Common | yes |
| UC03-F19 | INCORRECT | P3 | Docs claims contradicted by code | Both | partial |

### UC03-F01 — Case access control is a no-op · INCORRECT · P0 · Confirmed defect
- **Component / location:** `UC03/src/sub_agent/validator.py::DocumentValidationService.validate`, step 2 (:78-83); `guardrails/access.py::assert_case_access`; orchestrator `uc03_documents.py::_ensure_case_registered` (:77-79).
- **Observed:** `case_actor_ids=case.authorized_actor_ids | {request.requesting_actor_id}`. The caller is always added to the set, so the check can never raise. The adapter also adds any invoking actor to the case permanently.
- **Expected:** An actor not on the case is denied (DENY_ACCESS plus an ACCESS_DENIED audit event).
- **Evidence:** Probe P1 (`intruder-Z`, type CANDIDATE, was validated on `C-X`). No test calls `validate` with a foreign actor; only `determine_next_action` is unit-tested (`test_uc03_rules.py:262`). `ONBOARDING_COMMON_ARCHITECTURE.md:405` claims "Yes — authorized_actor_ids".
- **Root cause:** The union operand. The case-creation path authorizes whoever arrives first.
- **Impact:** Any caller of UC03's API can validate documents and mutate state on any case. In the orchestrated path this is mitigated only by the orchestrator's own role and tenant checks (`orch/api.py:247,257`; `get_case`), and the common doc's reliance on UC03 is unfounded.
- **Fix:** Check `request.requesting_actor_id in case.authorized_actor_ids` with no union. Grant access by an explicit HR action. Have the adapter register only the initiating actor and not extend the set on later calls.
- **Owner / deps:** UC03 owner. Common to update docs and add `require_role` before the UC03 call (as for UC01/UC02).
- **Tests:** A foreign actor gets DENY_ACCESS, an ACCESS_DENIED event, and no state mutation.
- **Acceptance:** Probe P1 returns DENY_ACCESS.
- **Common-layer fix without touching UC03:** No, though a compensating `require_role` is possible.

### UC03-F02 — No tenant isolation in UC03 stores · INCORRECT · P0 · Confirmed defect (standalone) / Risk (orchestrated)
- **Location:** `UC03/src/state/store.py::InMemoryCaseStore` (:23-46), `InMemoryAuditStore` (:48-61); `validator.py:60-75`. Orchestrator: one shared store per deps for all tenants (`uc03_documents.py:49-57`).
- **Observed:** Keyed by `case_id` only. `request.client_id` and `request.employee_id` are never compared with the stored case. `GET /audit/{case_id}` returns any case's events.
- **Expected:** Every read and write is scoped by `(client_id, case_id)`, and a mismatch is treated as not found.
- **Evidence:** Probe P7: `OTHER-TENANT` on the same case id sees `DUPLICATE_REQUEST` derived from tenant `default`'s submission (cross-tenant read and write). Orchestrated collision is unlikely because UC01 case ids are uuid4 (`UC01/src/sub_agent/open_case.py:125`).
- **Root cause:** The store interface has no tenant parameter.
- **Impact:** Tenant data leaks and state corrupts on the UC03 API. Latent in the orchestrator.
- **Fix:** Tenant-scoped store keys and client/employee consistency checks. In the adapter, namespace the UC03 case id as `f"{client_id}:{case_id}"`.
- **Owner:** UC03 owner. Common for the adapter namespacing.
- **Tests:** The same case id under two tenants stays independent, and an employee-id mismatch is rejected.
- **Acceptance:** Probe P7 yields an independent result (no DUPLICATE).
- **Common-layer fix:** Partial (yes for the orchestrated path, no for the UC03 API).

### UC03-F03 — Unauthenticated approval/audit API; HR approval asserted by the caller · INCORRECT · P1 · Confirmed defect
- **Location:** `UC03/src/api/uc03.py::submit_approval` (:165-200), `get_audit_trail` (:203-209); `validator.py` step 11 (:256-263); `hooks/approval.py::require_approval` (never called).
- **Observed:** Anyone can POST an APPROVED `COMPLETE_COMPLIANCE` record with a self-declared `actor_type=HR_USER`. Compliance is computed from the request's `hr_review_status`, not from the approval record; the record only feeds the `approval_status` display (:309-314).
- **Expected:** Approval requires an authenticated HR actor authorized on the case (`policy/approval_gates.yaml` COMPLETE_COMPLIANCE requires HR_USER), and compliance COMPLETE requires that record.
- **Evidence:** Probe P3 (`COMPLETE`, `approval_status: PENDING`, `next: NONE`). `test_scenario_26_all_valid_after_approval` asserts only `approval_status` (`test_uc03_validator.py:274`).
- **Root cause:** No auth dependency; the approval gate is advisory.
- **Impact:** Compliance can be marked complete with no HR decision. Not reachable via the orchestrator today, because the adapter leaves `hr_review_status` at its default.
- **Fix:** Authenticate the API, call `require_approval` before COMPLETE, and drop `hr_review_status` from the trusted request input.
- **Owner:** UC03.
- **Tests:** Compliance stays PENDING_APPROVAL without a record even when the request says APPROVED. An unauthenticated approval gets 401/403.
- **Acceptance:** Probe P3 returns PENDING_APPROVAL.
- **Common-layer fix:** No.

### UC03-F04 — Revalidation after correction impossible · INCORRECT · P1 · Confirmed defect
- **Location:** `validator.py` steps 5b/7/16 (:159-175, :316-324).
- **Observed:** Any RECEIVED submission, even a REJECTED one, is added to `received_documents`. The next submission of that type gets only `DUPLICATE_REQUEST`, all rules are skipped, and the result is HR_REVIEW and BLOCKED. There is no supersede or replace API.
- **Expected:** A replacement for a rejected or missing document is re-validated. Only a true duplicate of an accepted document is flagged.
- **Evidence:** Probe P2. Tests encode the current behaviour (`test_uc03_validator.py:195-201`, `test_uc03_workflow_e2e.py:376-392`).
- **Root cause:** Dedup is by document type, not by content or status.
- **Impact:** The core correction loop cannot complete, in UC03 standalone and in the orchestrator's same-process retries (probe B2).
- **Fix:** Record per-document submissions with a content hash and status. Re-validate when the prior status is REJECTED, MISSING or under correction. Add an explicit `resubmission_of`/supersede field.
- **Owner:** UC03.
- **Tests:** expired → corrected → VALID; identical bytes resubmitted → DUPLICATE.
- **Acceptance:** Probe P2's second line shows `VALID`.
- **Common-layer fix:** No. A fresh UC03 store per attempt would be a hack that also defeats dedup.

### UC03-F05 — Real UC03 can never let `validate_documents` COMPLETE · INCORRECT · P1 · Confirmed defect
- **Location:** `orch/adapters/uc03_documents.py` (:29-30, :100-111, :147-153); UC03 `validator.py:238-253`; `rules.py:160-163`.
- **Observed:** The adapter requires `document_status == "ACCEPTED"` and `compliance == "COMPLETE"`. UC03 never assigns `DocumentStatus.ACCEPTED` (the best case is UNDER_REVIEW). COMPLETE also needs `hr_review_status` APPROVED, which the adapter never sends.
- **Expected:** An agreed terminal-valid contract. For example, UC03 VALID plus HUMAN_CONFIRMATION maps to AWAITING_APPROVAL with a surfaced HR-review approval, and the HR decision leads to COMPLETE.
- **Evidence:** Probe B1 (8 valid documents give `blocked`). Grep shows `ACCEPTED` only at `enums.py:20`.
- **Root cause:** The adapter was written against an enum value, not against observed UC03 behaviour. No real-UC03 COMPLETE test exists.
- **Impact:** With real UC03, UC04 and UC05 are unreachable for every case.
- **Fix:** UC03 adds an HR-review decision operation that sets ACCEPTED. Common maps UNDER_REVIEW/PENDING_APPROVAL to AWAITING_APPROVAL plus a `pending_approvals` entry, and relays the decision.
- **Owner / deps:** Both (contract). Depends on F03 and F04.
- **Tests:** A real-UC03 orchestrated case reaches COMPLETE after an HR decision.
- **Acceptance:** A graph test without `_docs_double` reaches `equipment_access`.
- **Common-layer fix:** Partial (status mapping yes; a real acceptance needs UC03).

### UC03-F06 — No orchestrator path to submit or correct documents · MISSING · P1 · Confirmed defect
- **Location:** `orch/api.py::StartCaseRequest` (:54-57), route list (:1-11); `contracts.py::OnboardingCaseInput` (:20-63); `orchestrator_agent.py::initial_state` (:386-390); the adapter reads `intake_trigger["documents"]` (`uc03_documents.py:94`).
- **Observed:** Nothing ever populates `intake_trigger.documents`. In production every applicable document is MISSING, and `resume_case` re-runs with the same empty input.
- **Expected:** A tenant-scoped, authorized operation to attach or replace document references and metadata, followed by a resume.
- **Evidence:** Grep for `"documents"` in `orch/` finds only the adapter. Tests inject documents directly into state (`test_uc03_adapter.py:19-23`).
- **Root cause:** The integration was built around a state shape nothing produces.
- **Impact:** The orchestrated document stage is permanently BLOCKED. The UC-009 "re-runs with updated intake_trigger.documents" claim is false.
- **Fix:** Add `POST /cases/{id}/documents` (role `case_operator_roles`, references and metadata only, no raw bytes in state), persist it, and resume.
- **Owner:** Common (contract with UC03).
- **Tests:** Submit, then BLOCKED, then a corrected submit; only the changed document is re-validated.
- **Acceptance:** Via the API alone, a case moves from MISSING to the next state.
- **Common-layer fix:** Yes.

### UC03-F07 — Checklist context dropped between adapter and UC03 · INCORRECT · P1 · Confirmed defect
- **Location:** `uc03_documents.py::_validate_all` (:90-93 vs :100-111); `validator.py:151-157`; `checklist.py:34-43`; `default.yaml:29`.
- **Observed:** The adapter selects documents with `location`, but the request omits `location`, `role`, `grade`, `nationality`, `visa_required` and `sponsorship_applicable`. UC03 rebuilds the context without them, so a submitted Emirates ID for a UAE case gets `UNREQUESTED_SENSITIVE` and `APPLY_RETENTION_POLICY`. Location vocabulary also mismatches: `Dubai` is not `location:UAE`, so the Emirates ID is never required (probe B4). `grade` and `nationality` are never in the real `uc02_role_context` (`orchestrator_agent.py:391-394`).
- **Expected:** The same context is used for selection and validation, with a normalized location vocabulary.
- **Evidence:** Probe output `emirates_id ['UNREQUESTED_SENSITIVE'] APPLY_RETENTION_POLICY`; B4.
- **Impact:** UC03 recommends a retention/deletion action on a required identity document, and UAE requirements can be missed.
- **Fix:** Pass the full context fields in the request and map the location to the checklist's country vocabulary.
- **Owner:** Common.
- **Tests:** A UAE case with an Emirates ID validates as VALID; Dubai maps to UAE.
- **Acceptance:** The probe returns VALID for `emirates_id`.
- **Common-layer fix:** Yes.

### UC03-F08 — Out-of-scope NDA/policy-acknowledgement docs block the orchestrated stage · INCORRECT · P2 · Confirmed defect
- **Location:** `default.yaml:71-93` (`applies_to: [all]`, `check_attestation`, `requires_evidence_stored`); the adapter iterates all applicable docs (`uc03_documents.py:93-97`) and never passes `attestation_confirmed`.
- **Observed:** `nda` and `policy_acknowledgements` always get `MISSING_FIELDS` and `SEND_REPLACEMENT_REQUEST`, which is effectively a signature-return workflow, against the stated scope.
- **Expected:** These are excluded from the orchestrated checklist. Signature tracking must not be implemented.
- **Evidence:** Probe (`nda ['MISSING_FIELDS']`). `test_nda_or_signature_tracking_not_reintroduced` (`test_orchestrator_e2e.py:111`) checks `state.py` source only, so it misses this.
- **Impact:** The stage can never complete. Out-of-scope behaviour leaks into the lifecycle.
- **Fix:** A tenant or orchestrator exclusion list of document ids in the adapter, or a UC03 tenant checklist without them.
- **Owner:** Common (UC03 for a checklist variant).
- **Tests:** The orchestrated checklist excludes `nda` and `policy_acknowledgements`.
- **Acceptance:** B1 has no nda or policy rows.
- **Common-layer fix:** Yes.

### UC03-F09 — Extraction disconnected from validation · DISCONNECTED · P2 · Confirmed defect
- **Location:** `validator.py:47-52` (extractor stored and never used); `workflow.py:120-121`; `uc03_documents.py:46,52`; `extraction.py:309-317`.
- **Observed:** `/validate`, the UC03 graph and the orchestrator perform no extraction. `readable`, `required_fields_present`, `extracted_name` and `expiry_date` are trusted caller inputs. Only `/upload-and-validate` extracts.
- **Expected:** A document reference is passed to validation and UC03 extracts from it.
- **Evidence:** Grep (`self._extractor` appears only at :52).
- **Impact:** The orchestrated "validation" checks whatever booleans the caller asserts.
- **Fix:** A UC03 entry point `extract_and_validate(document_ref, ...)` backed by document storage (F10). The orchestrator passes references only.
- **Owner:** UC03 (Common to call it).
- **Tests:** The validator invokes the extractor when fields are absent.
- **Acceptance:** An orchestrated run with only `document_ref` yields extracted findings.
- **Common-layer fix:** No.

### UC03-F10 — Upload-path validation is vacuous or fabricated · INCORRECT · P2 · Confirmed defect
- **Location:** `UC03/src/api/uc03.py::upload_and_validate` (:63-122); `extraction.py::_extract_fields` (:152-158); `validator.py:183-186`.
- **Observed:**
  - `required_fields_present=bool(extracted_fields)` is true when every value is `None`.
  - Checklist field lists are never compared with the extracted fields.
  - `evidence_stored=True` is hard-coded, yet the temp file is deleted (:77) and nothing is stored (`DocumentStorageClient` unused).
  - `offer_accepted` and `onboarding_form_approved` are forced `True`.
  - The extractor's `injection_suspected` and `supported_language` are dropped.
  - No size limit, no type verification (`WRONG_TYPE` and `UNSUPPORTED_LANGUAGE` are never emitted), and no tests.
- **Expected:** Per-field presence checks against the checklist, real evidence storage before `evidence_stored`, and extraction flags propagated.
- **Evidence:** Probe P8; code lines cited.
- **Impact:** False VALID or COMPLETE results and fabricated evidence claims.
- **Fix:** Compute presence per required field, store via a storage port and set `document_ref` and `evidence_stored` from the result, then map the extraction flags to findings.
- **Owner:** UC03.
- **Tests:** Endpoint tests for empty fields, an injected PDF, an oversized file and the wrong type.
- **Acceptance:** P8 gives MISSING_FIELDS.
- **Common-layer fix:** No.

### UC03-F11 — Outstanding / required / evidence semantics wrong · INCORRECT · P2 · Confirmed defect
- **Location:** `validator.py:277`; `checklist.py:26-32`; `rules.py::check_evidence_stored` (:93-99); `default.yaml:68,97,108`.
- **Observed:**
  - `outstanding_requirements` always lists every applicable document (P5).
  - `required:false` documents count as outstanding.
  - `min_count` is ignored.
  - `evidence_status=STORED` is reported when evidence was not stored and not required (P4).
- **Expected:** Outstanding equals applicable required documents minus accepted ones, and evidence is reported as NOT_REQUIRED or NOT_STORED.
- **Impact:** HR and the orchestrator get misleading outstanding lists and evidence state.
- **Fix:** Pass `case.received_documents` (accepted only) to `get_outstanding`, honour `required` and `min_count`, and add a NOT_REQUIRED evidence state.
- **Owner:** UC03.
- **Tests:** Outstanding shrinks after an accepted document; evidence is NOT_STORED when absent.
- **Acceptance:** P5 shows `False`.
- **Common-layer fix:** No.

### UC03-F12 — In-memory only; orchestrated UC03 state lost on restart · PARTIAL · P2 · Confirmed defect
- **Location:** `store.py:23-80`; `uc03_documents.py:39-58`; `deps.py:46-49`; projection at `uc03_documents.py:134-142`.
- **Observed:** UC03 cases, received documents, approvals and audit live in process memory. The orchestrator persists only the 4-field projection. After a restart, UC03's dedup and audit history are gone and results differ (B2 vs B3). The UC03 audit of orchestrated runs is never exposed.
- **Expected:** A durable UC03 case, document and audit store, tenant-scoped.
- **Evidence:** Probes B2/B3. `UC03/specs/uc03_spec.md:333` acknowledges this.
- **Fix:** Implement durable `CaseStore`/`AuditStore` (the Protocols at `store.py:9-20`). `build_uc03_graph` already accepts injected stores (`workflow.py:111-116`), so Common can inject SQLite implementations.
- **Owner:** UC03 (store), Common (injection plus `persist_dir`).
- **Tests:** Validate, rebuild deps on the same persist dir, and confirm UC03 history and audit are present.
- **Acceptance:** B3 matches B2 semantics.
- **Common-layer fix:** Yes.

### UC03-F13 — Retries not idempotent · INCORRECT · P2 · Confirmed defect
- **Location:** `uc03_documents.py::_validate_all` (re-validates every document on every run); `validator.py:326-341`.
- **Observed:** Each re-run appends a `DOCUMENT_VALIDATED` event per document (16 after 2 runs, B2) and flips RECEIVED documents to DUPLICATE_REQUEST/BLOCKED. No idempotency key reaches UC03 (the request has none; only `correlation_id`). Orchestrator-side blockers are deduplicated (`_shared.py:46-51,66-71`). UC03 creates no review-task records, so there are no duplicate tasks, but comms drafts are regenerated on every run.
- **Expected:** A retry with unchanged input returns the prior result with no new side effects.
- **Fix:** UC03 accepts an idempotency key (case, document, content hash). Common skips re-validating documents whose submission fingerprint is unchanged.
- **Owner:** Both.
- **Tests:** Two identical runs give an identical result and 8 events, not 16.
- **Acceptance:** B2 equals B1 for passport.
- **Common-layer fix:** Partial.

### UC03-F14 — Result normalization gaps · PARTIAL · P2 · Confirmed defect
- **Location:** `uc03_documents.py:131-162`; `orch/api.py::case_view`.
- **Observed:**
  - `evidence_references` reads a field UC03 does not return, so it is always `[]` (B1).
  - No `pending_approvals` entry for UC03 HR review.
  - Blocker codes are raw document statuses (`UNDER_REVIEW`, `RECEIVED`), and findings and next actions appear only in free text.
  - `stage_errors` is unset for this stage.
  - `outstanding_tasks` is never cleaned up on completion.
  - `CaseView` omits `document_validation_result`.
  - Actor type is SYSTEM for a human HR id.
- **Expected:** Normalized codes (`MISSING_DOCUMENT`, `EXPIRED_DOCUMENT`, …), a UC03 review approval surfaced, evidence references from UC03, and stale items removed.
- **Fix:** Map `validation_findings` and `required_next_action` to orchestrator codes, add `add_pending_approval(agent="uc03-document-validation", …)`, take `document_ref` from the request, and set the actor type from roles.
- **Owner:** Common.
- **Tests:** Adapter projection assertions.
- **Acceptance:** B1 shows evidence references and finding codes.
- **Common-layer fix:** Yes.

### UC03-F15 — Missing identity fields make the stage FAIL instead of BLOCK · INCORRECT · P2 · Confirmed defect
- **Location:** `uc03_documents.py:64-75,100-102,125-129`.
- **Observed:** `employee_id=None` or `candidate_name=None` (the raw-email intake path, `contracts.py:36-37`) raises a pydantic error. The stage becomes FAILED with INTERNAL_ERROR, which is terminal (`orchestrator_agent.py:77`).
- **Expected:** BLOCKED with MISSING_DATA and owner `hr_admin`, resumable.
- **Evidence:** Probes B5/B6.
- **Fix:** Pre-validate the required identity fields in the adapter.
- **Owner:** Common.
- **Tests:** None-valued identity gives BLOCKED.
- **Acceptance:** B5/B6 give BLOCKED.
- **Common-layer fix:** Yes.

### UC03-F16 — Test gaps; the double emits states UC03 cannot produce · PARTIAL · P2 · Confirmed defect
- **Location:** `otests/_lifecycle.py:165-179`; `otests/test_uc03_adapter.py`; `otests/test_orchestrator_e2e.py:7-8,49,147`; UC03 `tests/integration/test_uc03_validator.py:250-296`.
- **Observed:**
  - Real UC03 is exercised only by 3 direct adapter tests, none of which reaches COMPLETE.
  - Every lifecycle test uses `_docs_double`, which emits ACCEPTED/COMPLETE.
  - The e2e tests never execute UC03.
  - UC03 has no tests for access denial, tenancy, revalidation or the upload endpoint; one audit assertion can never fail.
- **Expected:** At least one real-UC03 graph test per edge (block, correct, resume, complete), and a double constrained to UC03's real output space.
- **Fix:** Add a real-UC03 lifecycle test once F05 and F06 are fixed. Assert that the double's statuses are values UC03 emits. Fix the vacuous assertion.
- **Owner:** Both.
- **Acceptance:** CI includes a real-UC03 COMPLETE path.
- **Common-layer fix:** Yes for common tests.

### UC03-F17 — Scaffolding defined only · DISCONNECTED · P3 · Recommendation
- **Location:** §4 table.
- **Observed:** The MCP clients are mock-only and not imported; the connectors, hooks, context assembler, PII guard and policy YAMLs are unused; `SubAgent.run` raises NotImplementedError.
- **Expected:** Either wire them (document storage for F10, the approval hook for F03) or label them clearly as unused.
- **Fix:** Remove the scaffolding or document it, and wire `DocumentStorageClient` behind a port.
- **Owner:** UC03.
- **Common-layer fix:** No.

### UC03-F18 — In-process bridge risks · UNVERIFIED · P3 · Risk (inferred)
- **Location:** `uc03_documents.py:33-36,124`; `store.py:27,50`.
- **Observed:** `sys.path.insert(0, UC03 root)` makes UC03's generic top-level names (`src`, `mcp`, `guardrails`, `hooks`, `knowledge`, `tests`) win inside the orchestrator. Today `mcp` resolves to UC03's stub; the real `mcp` SDK is not installed, and litellm references it only under TYPE_CHECKING. Separately, `asyncio.run` per call shares an `asyncio.Lock` across loops and threads, and calling from a running loop raises. Harm was not reproduced.
- **Fix:** Run UC03 as a worker like UC01/UC04/UC05, or append to `sys.path` and give UC03 a namespaced package. Use a dedicated event-loop thread.
- **Owner:** Common (UC03 for packaging).
- **Tests:** Concurrent two-case validation; `import mcp` after the adapter runs.
- **Common-layer fix:** Yes.

### UC03-F19 — Docs claims contradicted by code · INCORRECT · P3 · Confirmed defect
- **Contradicted claims:**
  - `ONBOARDING_COMMON_ARCHITECTURE.md:405` (UC03 enforces identity) and `:380` (UC03 HR review listed).
  - Workspace analysis `:483` (hr_decision surfaced), `:486` ("Implemented and tested"), UC-009 (re-runs with updated documents).
  - Common `pyproject.toml:33` (`orchestration/settings.py` does not exist).
  - `test_orchestrator_e2e.py:7-8` (`_force_uc02_complete` does not exist).
  - UC03 `README.md` and `CLAUDE.md` describe an unrelated "Enterprise HR Onboarding Platform" sourcing/screening system.
  - `specs/uc03_spec.md:19,320` (MCP integration, `require_approval`).
  - `docs/uc03_workflow_guide.md:136` (access denied) and `:662` (ACCEPTED means HR approved, but it is never emitted).
- **Fix:** Correct the docs after the code fixes.
- **Owner:** Both.
- **Common-layer fix:** Partial (common docs only).

## 9. Required actions

| Priority | UC03 owner | Common layer (no UC03 code change) |
|---|---|---|
| 1 | F01 real access check; F02 tenant-scoped store keys | F07 pass full context and normalize location; F08 exclude NDA/policy-ack ids; F15 BLOCK on missing identity |
| 2 | F04 re-validation of corrected docs (content hash, supersede); F03 authenticated approvals with compliance driven by the ApprovalRecord | F06 document submit/correct endpoint plus resume; F02 namespace UC03 case ids per tenant; F01 add `require_role` before UC03 |
| 3 | F05 HR-review decision operation that emits ACCEPTED (agree the terminal-valid contract with Common) | F05 map UC03 UNDER_REVIEW/PENDING_APPROVAL to AWAITING_APPROVAL plus a `pending_approvals` entry, and relay the decision; F14 normalized codes and evidence refs |
| 4 | F09/F10 extract-and-validate entry point, per-field checks, real evidence storage; F11 outstanding/required/evidence semantics | F12 inject durable `CaseStore`/`AuditStore`; F13 skip unchanged submissions |
| 5 | F17 wire or remove scaffolding; F19 UC03 docs | F16 real-UC03 lifecycle test and a constrained double; F18 run UC03 as a worker or fix the `sys.path` order; F19 common docs |

Exit criterion for this integration: one orchestrated case with **real UC03** goes through submit → BLOCKED (expired) → corrected submit → AWAITING_APPROVAL (HR review) → HR decision → COMPLETE → `equipment_access`. It must use API calls only, survive a restart between steps, and produce no duplicate UC03 audit events.
