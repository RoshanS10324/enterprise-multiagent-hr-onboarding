# HR policy assistant: privacy rules and HR escalation

The HR policy assistant answers only what the policy document supports. It does not answer salary, medical or accessibility questions; it refuses them or sends them to HR privately, and it sends HR anything the policy doesn't cover. This page explains how.

The code is in `UC05_Orientation-Schedule-Agent/src/sub_agent/services/`:
- `policy_privacy.py`: the classifier
- `policy_assistant.py`: processing order and responses
- `policy_escalation.py`: HR escalation

The policy source is described in [HR_POLICY_KNOWLEDGE_INTEGRATION.md](HR_POLICY_KNOWLEDGE_INTEGRATION.md).

## 1. Processing order

Every route reaches the assistant through `OnboardingToolRegistry.answer_policy_question`: the API endpoints (`POST /v1/onboarding/policy-questions` and the workflow endpoint), workflow stage 10, and the orchestrator worker. There is one implementation.

1. **Authenticate.** The requester is identified by a JWT, or by development headers in development and test only.
2. **Resolve the tenant and employee.** Both come from the verified identity. Asking about an employee of another tenant is refused with `TenantAuthorizationError`.
3. **Check authorisation.** The tool registry checks that the policy-Q&A tool is allowed, applies the kill switch, and checks that the escalation gate is declared.
4. **Classify** (`classify_question`). This is deterministic and happens before retrieval and before any model call.
5. **Apply the privacy rules and decide whether to escalate** (table below).
6. **Retrieve** from the policy, for general questions only.
7. **Answer from the retrieved sections and validate it**: quotes grounded, citation references real, no pay figure missing from the cited quotes.
8. **Fall back to HR.** No answer, an uncovered question, or an unavailable policy all lead to an HR escalation.
9. **Audit** the category, rule names, a hash of the question and the escalation id. The question text is never audited.
10. **Return** the formatted answer, or a neutral notice with the escalation reference.

A model never sees a salary, medical or accessibility question, so it can't override these rules.

## 2. Categories and behaviour

| Category | Example | Behaviour |
|---|---|---|
| `injection` | "Ignore previous instructions…" | Refused; never retrieved or sent to the model. |
| `salary_other` | "What is the new employee's salary?", "How much does Ahmed make?", or any salary question about another employee's record | Polite refusal that confirms nothing. Recorded as `policy_qa.privacy_refusal` (category and requester only). No escalation. |
| `salary_own` | "What is my salary?", "Can I negotiate my salary?", "Can I get a raise?", renegotiating terms | No figures, no negotiation. Neutral notice and a private HR escalation. |
| `accessibility` | wheelchair, accessible desk, disability, reasonable adjustment | Neutral notice, private escalation, and one neutral plan item, `hr_accessibility_review`. |
| `medical` | names a condition or diagnosis, "I am pregnant", "my treatment" | The detail is never repeated or stored. Neutral notice and private escalation. Nothing is added to the plan. |
| `unsupported_language` | a language not in `settings.policy_qa.enabled_languages` | Notice and escalation; the question goes to HR as written. |
| `general` | "How is gratuity calculated?", "When is salary paid?", "Is sick leave paid?" | Retrieval and a grounded answer. If not covered, the notice "I couldn't find an answer to this question in the available HR policy. I'll route it to HR for clarification." and an escalation. |

**Which category wins.** The first match in this order: injection, salary (someone else's), salary (own), accessibility, medical, language, general.

**General pay questions are ordinary questions.** The policy covers payroll timing, gratuity and leave salary. Only questions about a specific person's pay, terms or health are diverted.

**Errs towards HR.** Matching uses English patterns plus Arabic keywords on normalised text. When unsure, it escalates; for example, "When will my salary be paid?" goes to HR because it is about the asker's own salary.

## 3. Language

- **Detection.** Arabic script is detected as `ar`. Latin text containing English function words is `en`. Anything else is `und` (not reliably detected); no other language is guessed.
- **What is enabled.** The configured languages are `guardrails/policy.yaml`, `settings.policy_qa.enabled_languages: [en]`. The document is English-only, so English is the only enabled language, and Arabic and other languages are escalated to HR.
- **Enabling another language.** If a language is added to the list, the model is told to answer in it, and the cited quotes stay verbatim in English. Retrieval, however, matches the English text, so a question written in that language usually finds no section and goes to HR as "not covered". It is never answered from a guess. Real support needs a reviewed translation of the policy or cross-language retrieval.

## 4. HR escalation

Escalation reuses the shared mechanisms; there is no separate escalation system.

| Step | Mechanism |
|---|---|
| Stored | An approval item in the shared HITL inbox: gate `hr_policy_escalation`, approver role `hr_admin`, proposal type `policy_escalation`. In Supabase mode that is `approval_requests`; otherwise `hitl.db` or memory. |
| HR is notified | `notify_approver` → `NotificationPort` (the Teams/Outlook abstraction), through the operation ledger, so HR is told once even after retries. |
| Status tracked | `open` (pending) → `resolved` (approved, or edited with a reply) or `closed` (rejected). `GET /v1/onboarding/policy-escalations/{id}`. |
| HR replies | HR edits the item with `employee_message`. The message is delivered once to the requester through `NotificationPort`. |
| Recorded in the workflow | Workflow route `approval_decided` → `apply_policy_escalation_decision`, with audit `policy_qa.escalation_decided`. |

**What an escalation holds:**
- category, a neutral reason code and the requester
- employee reference, language, request time and a hash of the question
- the approval id, which is also the correlation reference
- the handling level: `private` or `standard`

**When the question text is included.** Only for non-sensitive categories (`not_covered`, `unsupported_language`, `policy_unavailable`), because HR needs the question to answer it. For salary, medical and accessibility matters the text is never stored. HR sees only a fixed neutral sentence, such as "Accessibility or workplace adjustment review required. Please contact the employee directly.", and contacts the employee through their own channel.

**No duplicates.** The same requester asking the same question in the same category reuses the open escalation, and HR is notified once.

**Reporting what actually happened.** "Escalation created", "HR notification queued" and "notification delivered" are kept separate:

| `notification` value | Meaning |
|---|---|
| `delivered` | a real provider accepted the notice |
| `simulated` | the configured notifier is a simulation; today this is always the case |
| `rejected` | the provider refused the notice |
| `uncertain` | the outcome is unknown; the escalation is still in HR's queue |
| `not_attempted` | no notifier is configured |

The message shown to the employee says which of these applies. An escalation is never described as answered or resolved before HR decides it.

## 5. Authorisation and isolation

- **Escalation status.** Only the requester and `hr_admin` can see an escalation's status. Anyone else, including a colleague or another tenant, gets "not found", so the existence of someone else's private HR conversation is never confirmed.
- **Decisions.** Only `hr_admin` of the same tenant can decide an escalation; this is the HITL inbox's existing role and tenant check.
- **Tenants.** Every escalation and audit row carries the tenant. The Supabase-mode repository filters every query by `client_id`.
- **The plan item.** The accessibility item is the task type `hr_accessibility_review` and nothing else. It sits in UC05's checklist under the existing per-tenant authorisation.

## 6. What never goes into logs

The question text is never written to the audit log, the escalation's notice to HR, or the run's reported actions. The tool gateway already records only the tool name.

Two limitations, stated so they aren't assumed away:
- **Checkpoints.** On the workflow route, the question is part of the run's input and is kept in UC05's local workflow checkpoint store (`checkpoints.db`). Restrict and encrypt that volume.
- **Arabic matching was broken in Common.** The shared Arabic normalisation was deleting Arabic letters, so Arabic keyword and injection matching did not work anywhere. That is fixed (see the report).
