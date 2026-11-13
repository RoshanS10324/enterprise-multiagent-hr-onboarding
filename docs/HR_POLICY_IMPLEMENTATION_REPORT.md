# HR policy assistant: implementation report

**Date:** 2026-11-04. **Scope:** UC05's HR policy Q&A now answers only from `HR_Policy_Consultants.md`, applies deterministic privacy rules, and escalates privately to HR through the existing approval and notification path.

Related documents:
- [HR_POLICY_KNOWLEDGE_INTEGRATION.md](HR_POLICY_KNOWLEDGE_INTEGRATION.md): the policy source, retrieval and citations
- [HR_POLICY_PRIVACY_AND_ESCALATION.md](HR_POLICY_PRIVACY_AND_ESCALATION.md): the privacy rules and the escalation workflow

## 1. What was there before

- **Knowledge.** `PolicyKnowledgeBase` in UC05's `context_layer/knowledge/policy_index.py` ran over six synthetic documents in `context_layer/knowledge/policies/` (CLIENT-ACME, CLIENT-GLOBEX, global). It used Common's `KnowledgeIndex`, whose loader needs YAML front matter and `#` headings, which the real document doesn't have.
- **Instructions.** The prompt was the `hr-policy-answerer` v1 skill.
- **Deleted skills folder.** Commit `c6cf80e` ("DB Connection") deleted UC05's whole `context_layer/skills/` folder, including the loader and the unrelated `onboarding-welcome-writer` skill. UC05 could not import at all.
- **Privacy.** Only the prompt-injection check existed. Salary, medical, accessibility and language questions had no handling.
- **Escalation.** Nothing for policy questions. The building blocks already existed: the HITL inbox, `notify_approver` and `NotificationPort` with the operation ledger, and the audit log.

## 2. Files

**Created**

| File | Purpose |
|---|---|
| `HR-Onboarding-Common/src/hr_onboarding_common/context_layer/hr_policy.py` | Loads the document from the package (or `HR_POLICY_SOURCE_PATH`); parses sections, pages and the version; fails safely with no fallback |
| `UC05_Orientation-Schedule-Agent/src/sub_agent/services/policy_privacy.py` | Deterministic privacy classifier and language detection |
| `UC05_Orientation-Schedule-Agent/src/sub_agent/services/policy_assistant.py` | Processing order, neutral notices, answer or escalate |
| `UC05_Orientation-Schedule-Agent/src/sub_agent/services/policy_escalation.py` | Escalation on the HITL inbox; notice status; neutral plan item; applying HR's decision |
| `UC05_Orientation-Schedule-Agent/context_layer/skills/hr-policy-answerer/SKILL.md` | v2 instructions for the real policy: answer language, additional information, no salary or health topics |
| `UC05_Orientation-Schedule-Agent/tests/test_hr_policy_privacy_escalation.py` | 42 tests |
| `docs/HR_POLICY_KNOWLEDGE_INTEGRATION.md`, `docs/HR_POLICY_PRIVACY_AND_ESCALATION.md`, this report | documentation |

**Restored unchanged from git** (needed for UC05 to import; please confirm this is acceptable):
- `context_layer/skills/__init__.py`, the skill loader
- `context_layer/skills/onboarding-welcome-writer/SKILL.md`

**Modified**

| File | Change |
|---|---|
| Common `context_layer/knowledge.py` | `KnowledgeIndex.from_sections()`, an additive constructor; existing behaviour unchanged |
| Common `platform/guardrails.py` | **Bug fix (section 5).** The Arabic diacritics range was stripping all Arabic letters. |
| UC05 `context_layer/knowledge/policy_index.py`, `knowledge/__init__.py` | Retrieval over the real document: sections, multi-part questions, relevance gate, synonyms, heading-aware ranking, citations with pages and version |
| UC05 `services/policy_qa.py` | Answer language; `additional_information`; formatted message and policy references; output check for pay figures; extended `PolicyAnswer` |
| UC05 `tool_registry.py` | `answer_policy_question` now goes through the assistant; new `policy_escalation_status` and `apply_policy_escalation_decision` |
| UC05 `agent.py` | Decision route and node `apply_policy_escalation_decision`; stage 10 records the category and escalation, never the question |
| UC05 `api.py` | `GET /v1/onboarding/policy-escalations/{id}` |
| UC05 `guardrails/policy.yaml` (v11) | Gate `hr_policy_escalation`; tool `policy_qa.apply_escalation_decision`; `policy_qa.answer_question` moved to `write`; `settings.policy_qa.enabled_languages: [en]` |
| UC05 `context_layer/__init__.py`, `README.md` | Describe the real source |
| UC05 tests and `evaluation/run_evals.py` | Rewritten for the real policy |

**Removed:** `UC05_Orientation-Schedule-Agent/context_layer/knowledge/policies/` (6 synthetic policy documents). The real document is the only policy source.

## 3. Guardrails implemented

All of these are deterministic and run before retrieval and before any model call.

| Rule | Behaviour |
|---|---|
| Asker's own salary, renegotiation, a raise | Neutral notice and private escalation; no figures; not sent to the model |
| Another person's salary or terms | Polite refusal; nothing confirmed; privacy-safe audit; no escalation |
| Medical mention | Not repeated in the response, the plan, the audit log or the escalation; private escalation |
| Accessibility or adjustment request | Private escalation and one neutral plan item, `hr_accessibility_review` |
| Not covered by the policy | "I couldn't find an answer…" and an escalation that is tracked as `open` |
| Language not enabled (Arabic today) | Escalation; enabled languages are read from configuration, not assumed |
| Prompt injection | Refused before retrieval and the model; injected text inside the policy is dropped |
| Grounding | Quotes must appear verbatim in the cited section; unknown citation references are rejected; a pay figure not in a cited quote withholds the answer |

## 4. Escalation integration

- **Stored and decided through existing mechanisms.** Escalations are HITL inbox items (gate `hr_policy_escalation`, decided by `hr_admin`). In Supabase mode they land in `approval_requests` with no further work.
- **HR notified once.** The notice goes through `notify_approver` → `NotificationPort` on the operation ledger.
- **Status and access.** The status is `open`, `resolved` or `closed`. It can be read only by the requester and HR.
- **HR's reply.** HR replies through an edited decision that carries `employee_message`, which is delivered once through `NotificationPort`.
- **Duplicates.** An identical open escalation is reused rather than created again.
- **Honest delivery status.** `notification` is one of `delivered`, `simulated`, `rejected`, `uncertain` or `not_attempted`. Today it is always `simulated`, and the employee's message says so.

## 5. A bug fixed in shared code

`hr_onboarding_common.platform.guardrails._TASHKEEL` was `[ؐ-ًؚ-ٰٟۖ-ۭ]`. The ranges `U+0610–U+064B` and `U+061A–U+0670` contain every Arabic letter (`U+0621–U+064A`), so `normalize_arabic` deleted Arabic text entirely.

As a result:
- the Arabic prompt-injection rule (`ar_ignore_instructions`) could never match;
- Arabic keyword search and the protected-term scan saw only spaces.

The fix restricts the class to the diacritic blocks: `U+0610–061A`, `U+064B–065F`, `U+0670` and `U+06D6–06ED`. The commit history attributes the line to a commit of 2026-10-30.

## 6. Tests

Run on 2026-11-04 with Python 3.12.16. UC05 uses its worker environment with a freshly installed copy of Common; Common uses its own environment with the real UC01, UC04 and UC05 workers.

| Suite | Result |
|---|---|
| `tests/test_hr_policy_privacy_escalation.py` (new) | **42 passed** |
| `test_policy_knowledge_base.py`, `test_policy_qa_service.py`, `test_context_layer.py` (rewritten) | **51 passed** |
| UC05, whole suite | **644 passed, 3 skipped, 1 failed** |
| UC05 evaluations (`evaluation/run_evals.py`, offline) | **34 pass, 1 skip.** Policy citation cases: 8 of 8, no forbidden citations. The skipped case is model-dependent. |
| HR-Onboarding-Common, whole suite with real workers | see section 6a |

About the UC05 suite:
- **The failure was already there** (`test_orchestration_contract.py::test_status_vocabularies_…`): it expects Common's `Decision` values without `withdrawn`.
- **The 3 skips** are the opt-in live Supabase tests.

**What the new tests cover:**
- **Ordinary questions:** answered from the real sections, with the required format and references.
- **The six privacy scenarios:** own salary, renegotiation, another person's salary (five phrasings), medical, accessibility (including the neutral item and no duplicates), not covered, Arabic while disabled, Arabic salary, an enabled language, and configured languages.
- **Security:** escalation access limited to the requester and HR; HR-only decisions; three injection attempts; three attempts to rephrase around the salary rule; no personal records or prompt internals in answers; cross-tenant targeting; and the API route hiding other people's escalations.
- **Failures:** missing document, retrieval failure, LLM failure, escalation persistence failure, notifier unavailable, simulated notifier not reported as delivered, duplicate requests, and a retry after a partial failure.
- **Lifecycle:** HR's reply delivered once, a closed escalation sending nothing, and the workflow route never echoing a medical question.

### 6a. HR-Onboarding-Common

The first full run gave **589 passed, 13 failed**. Every failure was `ModuleNotFoundError: No module named 'cryptography'` inside the UC01 worker. UC01's merged Supabase/crypto work (`a86eb34`) added `cryptography>=43.0` to UC01's `pyproject.toml`, and the locally built UC01 worker environment predated it. UC01's declared dependencies were installed into that environment, and the suite was re-run: **590 passed, 12 failed**.

The remaining 12 all stop at real UC01 intake, with `ValidationError: OpenCaseRequest.placement — Field required`. UC01's rehire/placement change (`a86eb34`) made `placement` mandatory, and the orchestrator's UC01 adapter in Common doesn't send it. This mismatch between UC01 and the orchestrator predates this work and is not caused by it: none of the 12 tests involve policy Q&A, and the UC02 and end-to-end tests fail only because they run UC01 intake first. It needs a decision on where `placement` comes from (the intake request or UC01's defaults), and it was left unchanged.

Common's own unit tests for the changed modules (`context_layer`, `guardrails`) pass in that run.

## 7. Remaining blockers and known limits

1. **Arabic and other languages.** The policy exists in English only. Arabic is disabled and escalated. Enabling it would not produce answers, because retrieval over English text finds nothing for an Arabic question. It needs a reviewed Arabic version of the policy or cross-language retrieval.
2. **No effective date or approved version.** The document carries neither; the version is a content hash. HR should add front matter (`version`, `effective_date`) when they approve it.
3. **The classifier uses keywords.** It errs towards escalation, so some answerable questions go to HR (for example, "When will *my* salary be paid?"). Review with real employee questions and tune `policy_privacy.py`.
4. **The workflow checkpoint holds the question.** On the workflow route, the question is part of the run input kept in UC05's local `checkpoints.db`. It is not in audit, escalations or outputs. Encrypt that volume, or have callers use the direct route `POST /v1/onboarding/policy-questions`, which keeps no checkpoint.
5. **No protected channel exists for sensitive detail.** For salary, medical and accessibility matters, HR receives only a neutral request and contacts the employee directly.
6. **The skills folder is restored.** It was deleted in `c6cf80e`. Please confirm keeping it (the welcome-writer skill is needed by the welcome flow).

## 8. External integrations still mocked

| Integration | State |
|---|---|
| Teams and Outlook notices to HR, and HR's replies to employees | `MockNotifier` (simulated, reported as `simulated`) |
| LLM | FakeLLM in tests; the live LiteLLM gateway is unchanged and not exercised here |
| HITL inbox | the real shared inbox (SQLite or memory locally; Supabase `approval_requests` in Supabase mode, which is not live-verified) |
