# HR Onboarding Common Library — Architecture & Migration Design (Phase 2, revised)

> **Note (2026-10-27, agent v0.3.0):** this is a historical design record. Its paths and line
> numbers refer to the agent's earlier `src/onboarding_agent/` layout, which has since moved to
> `src/sub_agent/`, `connectors/` and `context_layer/`. `README.md` maps old locations to new ones.
> The security findings (S1–S6) and their fixes still apply as written.
>
> **Later update (2026-10-27):** NDA/HR policy acknowledgement and signature tracking -- the
> subject of finding **S3** below -- has since been retired from this agent's scope entirely
> (`checklist.verify_signature`, `Acknowledgement`, `src/sub_agent/signatures.py` and the
> `/v1/onboarding/*/acknowledgements*` routes no longer exist in the codebase). S3 is kept below,
> unedited, as a historical record of a security finding that was once real and fixed; it no longer
> describes current behaviour because the feature it concerns is gone, not because the finding was
> wrong. HR Policies Q&A is unrelated and remains fully active.

Status: **DESIGN ONLY — NOT IMPLEMENTED.** No files have been created at
`D:\Projects\D-ACT\hr-onboarding-common\`. No imports, dependencies, lockfiles,
or source files inside `HR-Onboarding-Agent` have been changed.
`hr-recruitment-common` and the Recruitment Agent have not been touched.
This revision responds to a security-focused review request and expands the
original Phase 2 design with a dedicated vulnerability review, a required
authentication-boundary specification, concrete (but still unimplemented)
fix designs, and explicit acceptance criteria. **Implementation remains
unauthorized; Phase 3 has not begun.**

This design follows directly from the accepted
[`COMMON_LIBRARY_ANALYSIS.md`](COMMON_LIBRARY_ANALYSIS.md) (Option A). Every
structural "keep / drop / modify" decision is backed by a grep-verified usage
count against the actual `HR-Onboarding-Agent/src` and `tests/` trees. Every
security finding below is backed by a specific file:line citation against the
actual shipped code, re-verified while preparing this revision — not assumed
or carried over from memory.

---

## Part A — Package structure (unchanged from the approved design)

### A.1 Proposed directory tree

```
D:\Projects\D-ACT\hr-onboarding-common\            # sibling of HR-Onboarding-Agent, NOT inside the recruitment repo
├── pyproject.toml
├── README.md
└── src/
    └── hr_onboarding_common/
        ├── __init__.py                # public re-export facade
        ├── agent.py                   # AgentContext, RunContext, BaseAgent (forked, trimmed, security-fixed)
        ├── api.py                     # create_app(), RunResult/ApprovalRef/DecisionBody (forked, security-fixed)
        ├── config.py                  # Settings, resolve_agent_home, get_settings (forked, trimmed)
        ├── contracts.py                # Autonomy, Purpose, Actor, Scope, Proposal, AgentResponse, ... (forked, trimmed, tenant-aware)
        ├── llm/
        │   ├── __init__.py
        │   ├── base.py                # LLMClient protocol, LLMError, LLMResult
        │   ├── callbacks.py           # prompt_hash, input_block helpers
        │   └── gateway.py             # LiteLLMGateway, FakeLLM, ModelTier
        ├── platform/
        │   ├── __init__.py
        │   ├── audit.py               # AuditLog (JSONL append-only)
        │   ├── hitl.py                 # HITLInbox, ApprovalItem, Decision, ReasonCode (trimmed, tenant-scoped, apply-failure-aware)
        │   ├── policy.py              # Policy, PolicyStore, PolicyViolation, GateDef
        │   ├── tools.py                # ToolGateway (kill-switch-enforcing)
        │   └── guardrails.py          # detect_injection, wrap_untrusted, quote_is_grounded,
        │                              # detect_pay_figures, scan_protected_terms (trimmed)
        ├── context_layer/
        │   ├── __init__.py
        │   └── knowledge.py            # KnowledgeIndex, Chunk, BrainContext (standalone, no Brain/Ontology)
        └── observability/
            ├── __init__.py
            ├── logging.py               # configure_logging, redact_secrets
            └── tracing.py               # configure_tracing (OTel no-op/console exporter)
```

The directory tree itself is **unchanged** from the originally approved
design. What changes in this revision is the internal content of `agent.py`,
`api.py`, `contracts.py`, `platform/hitl.py`, and `platform/tools.py` — each
now carries an explicit, enumerated security-fix design (Part B) in addition
to the trim decisions already approved (Part A.3). No new modules, no new
files, no change to the module list.

**Explicitly excluded from the library** (not ported, not stubbed — per the
constraint against placeholder modules): `connectors/ats/*`,
`context_layer/ontology.py`, `context_layer/memory.py` (`EpisodicMemory`),
`context_layer/__init__.py`'s `Brain` wrapper, `platform/skills.py`,
`platform/fairness.py`, `synthetic.py`, `testing.py`, `context_layer/data/`.
Reasons are unchanged from the original design (see the table previously
included; each exclusion is a confirmed zero-usage grep against onboarding
code, re-verified, not re-litigated in this revision).

### A.2 Component responsibilities

| Module | Responsibility | Consumers today |
|---|---|---|
| `agent.py` | `AgentContext` (settings/llm/policies/audit/hitl/connector bundle), `RunContext` (per-run proposal/action/exception recorder), `BaseAgent` (abstract run/submit-for-review harness) | `onboarding_agent/agent.py` (`OnboardingAgent(BaseAgent[...])`), `onboarding_agent/api.py` (`build_context()`), every service module via `ctx.connector`/`ctx.audit`/`ctx.hitl`/`ctx.policies` |
| `api.py` | `create_app()` FastAPI shell: `/healthz`, `/v1/runs`, `/v1/approvals*`; `RunResult`/`ApprovalRef`/`DecisionBody` request/response shapes | `onboarding_agent/api.py`'s top-level `app = create_app(agent_cls=OnboardingAgent, context=ctx, on_decision=on_decision)` |
| `config.py` | `Settings` dataclass (env-driven model tiers, timeouts, timezone, jurisdiction, `policy_dir`), `resolve_agent_home()` | `AgentContext.build()`, agent/test bootstrapping |
| `contracts.py` | `Autonomy`, `Purpose`, `Actor`, `WorkflowRef`, `Scope`, `PolicyRef`, `Proposal`, `AgentException`, `ActionTaken`, `Provenance`, `AgentResponse`, `BrainContext`, `BrainFeedback` | Every agent request/response boundary; `tests/conftest.py` builds `Actor`/`WorkflowRef`/`Scope` directly |
| `llm/` | Provider-neutral LLM gateway (`LiteLLMGateway`, `FakeLLM`, `ModelTier`), retry/fallback, prompt-hash/input-block helpers | `services/policy_qa.py`, `services/welcome.py` (`llm.structured()`, 2 direct call sites), `BaseAgent` error mapping (`LLMError`) |
| `platform/audit.py` | Append-only JSONL audit trail | `ctx.audit.record(...)` — used by every write-path service |
| `platform/hitl.py` | Human-in-the-loop inbox: propose → approve/reject/edit decision lifecycle | `scheduling.py`, `welcome.py`, `checklist.py` (every approval-gated flow) |
| `platform/policy.py` | YAML policy envelope loader + enforcement (`PolicyStore.require(...)`, `PolicyViolation`) | `AgentContext.build()`, `ToolGateway` |
| `platform/tools.py` | `ToolGateway` — allowlist + approval + audit wrapper around every connector/service call | `OnboardingToolRegistry`, `RunContext.tools` |
| `platform/guardrails.py` | Prompt-injection detection, untrusted-content wrapping, quote-grounding check, pay-figure/protected-term scanners | `services/policy_qa.py` |
| `context_layer/knowledge.py` | `KnowledgeIndex` — generic BM25 retrieval over tiered markdown chunks; `BrainContext` chunk record | `onboarding_agent/knowledge/policy_index.py` |
| `observability/logging.py` / `tracing.py` | Structured logging / OTel tracer setup | `create_app()` lifespan |

### A.3 Dependency / usage mapping (structural trims — unchanged decisions)

These decisions are unchanged from the approved design; each is a confirmed
zero-usage grep against `HR-Onboarding-Agent/src` and `tests/`:

| Area | Decision |
|---|---|
| `AgentContext.brain`, `.skills`, `.ats` | Drop (0 call sites each) |
| `RunContext.llm()`, `.brain_search()` | Drop (0 call sites; depend on dropped fields) |
| `RunContext.brain_feedback()` | Keep (unused today, zero-cost, no dependency on dropped fields) |
| `Purpose` enum | Redefine with only `ONBOARDING` (every call site uses this value exclusively; see §A.4 for one addition in this revision) |
| `Scope.requisition_ids` | Rename → `subject_ids` (one call site, `tests/conftest.py`, generic usage) |
| `platform/guardrails.py` | Trim to the 5 actively-imported functions + shared helpers; drop `redact_pii`, `screen_inclusive_language` (0 call sites) |
| `platform/hitl.py` `ReasonCode.WRONG_BAND` | Drop (recruitment-only, 0 call sites) |
| `context_layer/` | Keep only `knowledge.py` (`KnowledgeIndex`, `BrainContext`); drop `Brain`, `Ontology`, `EpisodicMemory` |
| `api.py` `default_context()` | Drop (unused by onboarding; hard-coupled to `ATS_ADAPTER`) |
| `config.py` `skills_dirs`, `context_dir`, `PACKAGE_DATA` | Drop (supported only the dropped `Brain`/`SkillLibrary`) |
| `langgraph`, `langchain-core` | Move from the library's dependency list to `HR-Onboarding-Agent/pyproject.toml` directly (the library itself never imports them; `src/onboarding_agent/agent.py:19` does) |

### A.4 One addition to `contracts.py` in this revision

`Purpose` keeps only `ONBOARDING` as previously designed. **New in this
revision:** `Actor` gains a `client_id: str | None = None` field (today,
upstream `Actor` has no tenant representation at all — confirmed,
`hr_recruitment_common/contracts.py:61-64`). This is the load-bearing change
behind the security fixes in Part B — without it, there is no place in the
shared contract layer to carry "which tenant does this actor act for,"
which is the root cause of findings S1 and S4 below.

---

## Part B — Security review

### B.1 Scope and method

Six categories of concern were specified for review. Each was checked
against the actual code in `HR-Onboarding-Agent` and
`hr-recruitment-common` as they exist today — not assumed from the earlier
design pass. **All six are confirmed present in the shipped code today,
independent of this migration.** None are hypothetical, and none were
introduced by (or are a consequence of) the proposed fork — they predate it.
This section states that explicitly for each finding, then Part B.4
specifies the fix required so the fork does not silently re-ship them as
"accepted behavior."

### B.2 Findings

**S1 — Cross-tenant approval access and decisions (Critical)**

- `ApprovalItem` (`hr_recruitment_common/platform/hitl.py:41-61`) has **no
  tenant/`client_id` field at all**.
- `HITLInbox.get()` (`hitl.py:102-107`), `.list()` (`hitl.py:109-121`), and
  `.decide()`/`_decide_locked()` (`hitl.py:123-156`) filter only by `id`,
  `status`, `gate`, `workflow_instance` — never by tenant.
- `/v1/approvals`, `/v1/approvals/{id}`, `/v1/approvals/{id}/decision`
  (`hr_recruitment_common/api.py:122-143`) authenticate via `actor()`
  (`api.py:100-101`), which reads only `X-User-Id`/`X-User-Roles`. There is
  no `X-Client-Id` parameter and no tenant check on these routes at all.
- Meanwhile `OnboardingToolRegistry` **does** tenant-check at
  proposal-creation time (`_authorize_employee`/`_authorize_client`,
  `tool_registry.py:68-77`, invoked from `schedule_session`,
  `propose_buddy_assignment`, etc.) — but the approval items those calls
  create live in the same `ctx.hitl` instance that the tenant-blind
  `/v1/approvals*` routes expose (`onboarding_agent/api.py:60,67-81`: both
  route groups share one `AgentContext` by design).
- **Impact:** any caller who can reach the API at all — regardless of which
  tenant they belong to — can list every pending approval across every
  tenant and approve, reject, or edit (`edited_payload`) any other tenant's
  approval item. An edited payload is then applied to the *owning* tenant's
  real records by `on_decision()` (`onboarding_agent/api.py:67-77`).
- **Root cause location:** `hr_recruitment_common` (`HITLInbox`,
  `create_app()`'s approval routes). **Cannot be fixed by editing the
  original** (constraint: `hr-recruitment-common` must not be modified) —
  must be fixed in the fork.

**S2 — Missing or insufficient role-based authorization (High)**

- `HITLInbox._decide_locked()` *does* check `item.approver_role not in
  approver_roles` (`hitl.py:139`) — a real check exists in code.
- But `approver_roles` is sourced entirely from the unauthenticated,
  client-supplied `X-User-Roles` header (`hr_recruitment_common/api.py:100-101`;
  `onboarding_agent/api.py:84-91`). Any caller can self-declare `hr_admin`
  and pass the check — this is a check with no trustworthy input, not a
  missing check.
- Separately, some registry operations have **no role requirement at all**
  beyond tenant scoping — e.g. `verify_signature`
  (`tool_registry.py:143-147`) has no approver-role gate (contrast with
  scheduling's `team_lead_id`/`buddy_id` specific-approver enforcement).
  Any actor who can reach the signature endpoint, in any role, can mark a
  document signed (compounds with S3 below).
- **Root cause:** split — the header-trust problem is in
  `hr_recruitment_common` (fixed via the fork, see B.3); the missing
  role gate on `verify_signature` is in `onboarding_agent` (fixed in the
  agent repo, see B.4).

**S3 — Forged acknowledgement evidence (Critical)**

- `checklist.verify_signature_event`
  (`HR-Onboarding-Agent/src/onboarding_agent/services/checklist.py:95-109`)
  accepts a free-text `verified_source: str` from the caller and checks only
  that its `:`-prefixed channel name is one of
  `TRUSTED_VERIFICATION_SOURCES = ("docusign", "hellosign",
  "hr-admin-manual-review")` (`checklist.py:33`) — a **string-pattern
  match**, not a cryptographic signature, provider webhook callback, or any
  out-of-band confirmation that the named channel actually processed this
  document.
- `SignatureBody` (`onboarding_agent/api.py:112-114`) takes `verified_by`/
  `verified_source` directly as caller-supplied JSON with no server-side
  binding to a real DocuSign/HelloSign event or an authenticated HR
  reviewer identity.
- `checklist.verify_signature` is in `policy.yaml`'s plain `write` list,
  not `write_requires_approval` (`guardrails/policy.yaml:25`) — it is not
  HITL-gated; the string check is the *only* control.
- **Impact:** any caller with onboarding API access can mark any employee's
  any acknowledgement as signed by supplying the literal string
  `"docusign:envelope-998"`, with no real signing event having occurred.
- **Root cause:** entirely in `onboarding_agent`
  (`checklist.py`/`api.py`/`policy.yaml`) — **must be fixed in the agent
  repository**, not the library.

**S4 — Cross-tenant access through `/v1/runs` (Critical)**

- `/v1/runs` (`hr_recruitment_common/api.py:111-120`) builds
  `Scope(requisition_ids=body.requisition_scope or [body.instance])`
  directly from caller-supplied request-body fields, with no server-side
  check that `body.instance` (the workflow instance — an employee id, for
  onboarding) belongs to the caller's tenant. The route's `actor()`
  dependency (`api.py:100-101`) takes no client-id parameter at all.
- Contrast with `OnboardingToolRegistry`, which calls
  `_authorize_employee`/`_authorize_client` before touching any record.
- **Impact:** a caller can invoke `OnboardingAgent.run()` (the LangGraph
  welcome-drafting path) against any employee id, including ones belonging
  to a different tenant, with nothing validating ownership before the run
  executes or before the resulting proposal is created.
- **Root cause:** `hr_recruitment_common.api.create_app()`'s `/v1/runs`
  route. **Cannot be fixed by editing the original** — must be fixed in the
  fork.

**S5 — Approval decisions consumed before follow-up actions succeed (High)**

- `HITLInbox.decide()` (`hitl.py:123-131`) calls `_decide_locked()`, which
  durably commits `item.status = APPROVED/EDITED/REJECTED` to SQLite
  (`self._save(item)` → `conn.commit()`, `hitl.py:148-152`) **before**
  `decide()` invokes `for listener in self._listeners: listener(item)`
  (`hitl.py:129-130`).
- The registered listener is `on_decision()`
  (`onboarding_agent/api.py:67-77`), which performs the real side effect:
  `scheduling.apply_decision(...)` (books the calendar slot),
  `scheduling.apply_buddy_decision(...)` (writes `buddy_id`),
  `welcome.apply_decision(...)` (prepares the send-ready draft).
- If any of these raise (e.g. `CalendarConflictError`/`CalendarBookingError`
  — standing in for a real provider that can fail), the exception
  propagates out of `HITLInbox.decide()` with **no handler registered for
  it** in `create_app()` → an unhandled 500. The SQLite row is already
  committed as the terminal decision, and `_decide_locked()` permanently
  rejects any further decision attempt (`"already decided"`, `hitl.py:138`)
  — there is no retry path and no stored indication that the real-world
  action never happened.
- **Impact:** an HR admin approving a session booking can get a 500 while
  the system of record shows the approval as permanently APPROVED, the
  calendar event was never created, and there is no way to re-trigger it
  short of fabricating a brand-new proposal.
- **Root cause:** `hr_recruitment_common.platform.hitl.HITLInbox.decide()`'s
  commit-then-notify ordering with no outcome tracking. **Cannot be fixed
  by editing the original** — must be fixed in the fork.

**S6 — Kill-switch and policy-enforcement bypass (High)**

- `Policy.enabled` (`hr_recruitment_common/platform/policy.py:33`, "per-agent
  kill switch") is read in exactly one place in the entire codebase:
  `hr_recruitment_common/agent.py:161`, inside `BaseAgent.run()`.
- `ToolGateway.call()` (`hr_recruitment_common/platform/tools.py:25-35`) —
  the enforcement point for every `OnboardingToolRegistry` method, i.e.
  effectively all onboarding functionality reachable via
  `/v1/onboarding/*` (intake, checklist, scheduling, buddy assignment,
  policy Q&A) — **never reads `policy.enabled`**. It only calls
  `self.policy.tool_mode(tool)` (allowlist + autonomy check).
- This is already known and explicitly documented in the existing test
  suite: `HR-Onboarding-Agent/tests/test_tool_registry.py:96-100`,
  `test_disabled_agent_kill_switch_blocks_everything` — whose body does not
  actually assert anything is blocked; it asserts `disabled.enabled is
  False` and comments *"enabled is advisory at the BaseAgent.run layer
  (kill switch), not checked by ToolGateway itself."*
- **Impact:** flipping the kill switch (`enabled: false` in
  `guardrails/policy.yaml`) stops the `/v1/runs` path but has **zero
  effect** on `/v1/onboarding/*` — intake, checklist writes, scheduling,
  buddy assignment, and policy Q&A all continue operating normally. Since
  most of onboarding's functionality runs through the registry/ToolGateway
  path, the kill switch is largely non-functional for this agent as
  currently composed.
- **Root cause:** `hr_recruitment_common.platform.tools.ToolGateway.call()`
  missing an `enabled` check. **Cannot be fixed by editing the original** —
  must be fixed in the fork.

### B.3 Findings summary table

| # | Finding | Severity | Root cause location | Fixable by editing the original? | Where it must be fixed |
|---|---|---|---|---|---|
| S1 | Cross-tenant approval access/decisions | Critical | `hr_recruitment_common` | No | Fork (`hr_onboarding_common`) |
| S2 | RBAC backed by unauthenticated headers + missing role gate | High | Split | Partially | Fork (header-trust model) **and** agent repo (`verify_signature` role gate) |
| S3 | Forged acknowledgement evidence | Critical | `onboarding_agent` only | N/A — already in the repo we own | Agent repo only |
| S4 | Cross-tenant access via `/v1/runs` | Critical | `hr_recruitment_common` | No | Fork |
| S5 | Decision consumed before action succeeds | High | `hr_recruitment_common` | No | Fork |
| S6 | Kill-switch bypass in `ToolGateway` | High | `hr_recruitment_common` | No | Fork |

**None of these are migration defects** (mistakes the fork process itself
could introduce). All six are pre-existing in the code shipped today,
confirmed by direct inspection, independent of whether the migration ever
happens. See Part D.3 for the separate, much smaller list of risks the
migration process itself could introduce.

### B.4 Required fixes — design (still unimplemented; for approval)

These are **proposed designs**, not code. Nothing below has been written to
any file. Each fix is scoped to the smallest change that closes the finding
without redesigning unrelated behavior.

**Fix for S1 + S4 (shared root cause: no tenant representation in the
contracts):**

1. `contracts.Actor` gains `client_id: str | None = None` (Part A.4). This
   is the single source of tenant identity threaded through both the
   LangGraph path and the registry path.
2. `platform.hitl.ApprovalItem` gains a required `client_id: str` field.
   Every current construction site must supply it:
   `BaseAgent.submit_for_review()` (derives it from `request.actor.client_id`)
   and every `OnboardingToolRegistry` method that creates a proposal
   (derives it from `self.actor.client_id`, retiring the separately-threaded
   `actor_client_id` constructor parameter as redundant once `Actor` carries
   it — see B.5 agent-repo changes).
3. `HITLInbox.list()` gains a `client_id: str | None = None` filter;
   `HITLInbox.get()` and `.decide()` gain a required `client_id: str`
   parameter that raises `HITLError` (mapped to 404, not 403, to avoid
   confirming cross-tenant existence) when the item's `client_id` does not
   match.
4. The forked `api.py`'s `/v1/approvals`, `/v1/approvals/{id}`,
   `/v1/approvals/{id}/decision`, and `/v1/runs` routes require
   `X-Client-Id` the same way `onboarding_agent/api.py`'s own
   `_actor_and_client()` already does, and pass it through to the
   `HITLInbox` calls above.
5. The forked `create_app()` gains an optional
   `tenant_check: Callable[[AgentContext, str, str], None] | None` hook,
   called as `tenant_check(ctx, body.instance, client_id)` before
   `agent.run()` executes. This keeps `create_app()` domain-agnostic (it
   doesn't know that "instance" means "employee id" for onboarding) while
   letting `onboarding_agent/api.py` supply a hook that reuses the exact
   tenant check `_authorize_employee` already performs.

**Fix for S5 (decision consumed before action succeeds):**

1. `ApprovalItem` gains `apply_error: str | None = None`.
2. `HITLInbox.decide()` wraps the listener-notification loop in a
   try/except: on a listener exception, it records `item.apply_error =
   str(exc)`, re-saves the item (status remains the human's genuine
   decision — that part is real and must not be silently undone), writes
   an audit event `hitl.apply_failed`, and raises a distinguishable
   exception (e.g. `HITLApplyError`) that the API layer maps to a
   502-style response making the discrepancy visible, instead of an opaque
   500.
3. `HITLInbox` gains `retry_apply(approval_id, client_id)`: re-invokes the
   listeners for an item whose `apply_error` is set, without requiring a
   new approval. Each `apply_decision`-style function in `onboarding_agent`
   must be confirmed idempotent-under-retry at implementation time (not
   assumed here) before this is wired up — several already follow an
   idempotent-by-existing-draft/idempotent-by-already-assigned pattern
   elsewhere in this codebase, but each must be individually checked, not
   presumed safe by analogy.

**Fix for S6 (kill-switch bypass):**

1. `ToolGateway.call()` gains, as its first check:
   `if not self.policy.enabled: raise PolicyViolation(f"{self.policy.policy}:
   agent is disabled (kill switch)")`, before the existing
   `tool_mode()` allowlist check. This is a minimal, single-method change
   that makes the kill switch actually block registry-backed operations,
   closing the gap directly.

**Fix for S2 (header-trust / RBAC), library-side portion — see Part B.5
for the required authentication boundary this cannot fully close on its
own:**

1. The forked `create_app()` is structured so the `actor()`/`_actor_and_client()`-style
   dependency is a single, clearly named, overridable function (not inlined
   per-route), so that when a real identity verifier exists it can be
   substituted without touching route signatures. This is an interface
   seam, not a working authentication mechanism — see B.5 for why a real
   fix requires infrastructure this design cannot invent.

### B.5 Required trusted authentication boundary and fail-closed behavior

**`X-User-Id`, `X-User-Roles`, and `X-Client-Id` must not be treated as
authentication.** They are currently — in both the original and, absent
further work, the fork — read as plain, unverified HTTP headers
(`hr_recruitment_common/api.py:100-101`,
`onboarding_agent/api.py:84-91`). Anyone who can send an HTTP request to
the service can set any value for any of them. The existing
`TODO(identity)` comment in `hr_recruitment_common/api.py:10-11` already
acknowledges this; this design does not invent a new problem, but it must
not silently re-ship the gap as "fine" either.

**Required boundary (infrastructure, not code this design can create):**
these headers must be set **exclusively** by a trusted enforcement point
sitting in front of this FastAPI app — an API gateway or reverse proxy
that:

1. Authenticates the caller by a real mechanism (JWT validated against an
   IdP's JWKS, mTLS, or a server-validated session) — not by trusting
   anything the client sends directly.
2. Derives `X-User-Id`/`X-User-Roles`/`X-Client-Id` from the *validated*
   token/session claims, then sets them itself, **stripping** any
   client-supplied versions of the same headers before forwarding.
3. Is the only network path to the app — the app's own listening port must
   not be directly reachable by any caller who could bypass the proxy and
   inject these headers itself.

**No such gateway/IdP integration exists in this project today** (confirmed
— no JWT validation, JWKS fetch, or IdP client appears anywhere in either
repo). This design does **not** fabricate one — per the constraint against
claiming unsupported integrations exist. It states the requirement and
marks it as a **deployment blocker for any non-local use**, not something
closed by this document or by Phase 3 alone.

**Fail-closed behavior, today vs. required:**

- Today, `Header(...)` (no default) makes `x_user_id`/`x_client_id`
  *required* — FastAPI returns 422 if they're absent. That is fail-closed
  for the "missing" case only.
- There is **no defense against a forged-but-present** header — nothing
  validates authenticity. This is the gap S1/S2/S4 exploit.
- **Required for the fork to be considered safe for anything beyond local,
  single-tenant, trusted-network use:** the app must fail closed
  (401/403) whenever identity cannot be cryptographically verified, not
  merely whenever a header is absent. Until a real verifier is wired in
  (gateway or in-app JWT validation), this requirement is **not met**, and
  every tenant-isolation claim in this document and in
  `docs/API_ENDPOINT_TEST_EXAMPLES.md` must be read as "enforced at the
  application layer assuming a trusted caller," not as a substitute for
  authentication.

This is listed as an explicit, named gap in Part D's acceptance criteria —
not something the fork can resolve by itself, but something the fork must
not make harder to fix later (the `tenant_check` hook and the
single-dependency-function structure in B.4 are designed specifically so a
real verifier can be substituted without another redesign).

---

## Part C — Migration, validation, and compatibility

### C.1 Migration sequence (for Phase 3/4, not executed now)

1. **Scaffold the new package** at `hr-onboarding-common\` with the
   structure in A.1, porting modules per A.3/A.4, **including the B.4
   security fixes as part of the initial port, not as a later patch** —
   the fork must not ship S1/S4/S5/S6 as working-as-designed. `hr-recruitment-common`
   is not touched.
2. **Build the new package standalone** (`uv build`/`uv sync`) to confirm
   no residual recruitment-specific import.
3. **Update `HR-Onboarding-Agent/pyproject.toml`** (swap dependency, add
   `langgraph`/`langchain-core` directly, update `[tool.uv.sources]`).
4. **Repoint imports** across `onboarding_agent/`: `agent.py`, `api.py`,
   `tool_registry.py`, every `services/*.py`, `knowledge/policy_index.py`,
   `connectors/*/base.py`.
5. **Apply the agent-repo-side fixes (Part B.4/B.5's agent-repo column)**:
   - Add a role gate (require `hr_admin`, or move to
     `write_requires_approval`) to `checklist.verify_signature` (S3).
   - Retire `OnboardingToolRegistry`'s separate `actor_client_id` parameter
     in favor of `actor.client_id` now that `Actor` carries it, updating
     every call site in `onboarding_agent/api.py`'s `_registry()`.
   - Wire the new `tenant_check` hook into `create_app(...)` for `/v1/runs`.
   - Update `tests/conftest.py`: import paths, `Scope(requisition_ids=...)`
     → `Scope(subject_ids=...)`, and add `client_id=` to every `Actor(...)`
     / `ApprovalItem`-producing fixture.
6. **Run the full test suite plus the new regression tests from C.3**
   before declaring migration complete.
7. **Leave `hr-recruitment-common` and the Recruitment Agent dependency
   graph completely unchanged** throughout.

Step 5's security fixes are **not optional follow-up work** — per the
instruction that known vulnerabilities must not be copied in as accepted
behavior, they are part of what makes Phase 3 "complete," not a
nice-to-have deferred to a later phase.

### C.2 Compatibility review (constraint item 7)

| Component | Compatible with the trimmed + fixed design? | Notes |
|---|---|---|
| `AgentContext` | Yes | Unchanged fields preserved; trimmed fields have zero call sites |
| `BaseAgent` | Yes, with one addition | `submit_for_review()` must now populate `ApprovalItem.client_id` from `request.actor.client_id` — a required, not optional, change (S1 fix) |
| `create_app()` | Yes, with fixes | Gains `X-Client-Id` enforcement on approval/run routes and the `tenant_check` hook (S1/S4 fix); `default_context()` dropped (unused) |
| Contracts | Yes, with three deliberate changes | `Purpose` redefined, `Scope.requisition_ids`→`subject_ids`, **new:** `Actor.client_id` added |
| LLM gateway | Yes, unchanged | No coupling found |
| Approvals (`platform/hitl.py`) | Yes, with required fixes | `ApprovalItem` gains `client_id`/`apply_error`; `HITLInbox` gains tenant-scoped `get`/`list`/`decide` and `retry_apply`; `ReasonCode.WRONG_BAND` dropped |
| `ToolGateway` | Yes, with required fix | Gains the `policy.enabled` check (S6 fix) |
| Audit logging | Yes, unchanged | No coupling found |
| Guardrails | Yes, trimmed subset | Unchanged from original design |
| Policy enforcement (`policy.yaml`) | Yes, with one agent-repo change | `checklist.verify_signature` should move to `write_requires_approval` or gain a role check (S3 fix) |
| Knowledge retrieval | Yes, promoted to standalone | Unchanged from original design |

No incompatibility blocks the design. The security fixes change behavior
**intentionally and are documented as breaking changes** in C.3 below —
they are not silent.

### C.3 Breaking changes and required regression tests

| Change | Breaking? | Why | Required regression test |
|---|---|---|---|
| `Scope.requisition_ids` → `subject_ids` | Yes (1 call site) | Naming cleanup, no behavior change | Existing `conftest.py` fixtures still construct scopes correctly post-rename |
| `Purpose` redefined (ONBOARDING-only) | No (0 other values used) | Removes unused recruitment values | N/A — no test references the removed values |
| `Actor.client_id` added | No (additive, optional) | Required to carry tenant identity through contracts | New: constructing `Actor` without `client_id` still works; `submit_for_review()` with `client_id=None` is rejected or clearly flagged, not silently accepted as "no tenant" |
| `ApprovalItem.client_id` added, required | **Yes** — every construction site must supply it | Closes S1 | New: **cross-tenant approval test** — tenant A creates a proposal, tenant B's `GET /v1/approvals`, `GET /v1/approvals/{id}`, and `POST /v1/approvals/{id}/decision` must all fail (404/403), not succeed |
| `HITLInbox.get/list/decide` require `client_id` | **Yes** | Closes S1 | Same cross-tenant test above, plus: same-tenant decide still succeeds unchanged |
| `create_app()` requires `X-Client-Id` on `/v1/runs`/`/v1/approvals*` | **Yes** — any caller not sending this header now gets 422 instead of a response | Closes S1/S4 | New: existing `/v1/runs` smoke tests updated to send `X-Client-Id`; a test confirming its absence is rejected |
| `create_app()` `tenant_check` hook wired for `/v1/runs` | **Yes** — cross-tenant runs now rejected | Closes S4 | New: **cross-tenant `/v1/runs` test** — running against another tenant's employee id must fail (403), not execute |
| `ToolGateway.call()` checks `policy.enabled` | **Yes** — registry calls now fail when the agent is disabled | Closes S6 | New: **kill-switch-blocks-registry test** replacing the existing `test_disabled_agent_kill_switch_blocks_everything`, which must now assert a blocked call (`PolicyViolation`), not merely `disabled.enabled is False` |
| `HITLInbox.decide()` catches listener exceptions, adds `apply_error` | **Yes** — a failed apply now raises a distinguishable error instead of a bare 500, and leaves `apply_error` set | New: **apply-failure-visibility test** — force the calendar/email connector to raise during `on_decision`, confirm the approval record shows `apply_error` set and a `retry_apply` call succeeds once the fault is cleared |
| `checklist.verify_signature` role-gated or HITL-gated (agent repo) | **Yes** — a bare `verified_source` string from an unprivileged caller is no longer sufficient | Closes S3 | New: **forged-acknowledgement-rejected test** — a non-`hr_admin` actor (or an unapproved HITL item, depending on which fix is chosen) attempting to verify a signature must be rejected |
| `OnboardingToolRegistry` drops the separate `actor_client_id` param | **Yes** (constructor signature change, agent repo only) | Redundant once `Actor.client_id` exists | Existing registry tests updated to construct via `actor=Actor(..., client_id=...)` only |

Every row above must have its listed regression test passing before
Phase 3/4 implementation work is considered complete — see Part D.4.

### C.4 Validation plan (for Phase 5, not executed now)

1. **Unit/contract parity**: existing suite passes with only the enumerated
   breaking changes above as test-code deltas — not unexplained failures.
2. **Guardrail regression**: `detect_injection`, `quote_is_grounded`,
   `scan_protected_terms`, `detect_pay_figures` test cases unchanged.
3. **Approval / audit / tenant-isolation checks**: existing HITL/scheduling/
   buddy/attendance tests pass, **plus every new regression test in C.3**.
4. **API-level smoke test**: workflows A–F from
   `docs/API_ENDPOINT_TEST_EXAMPLES.md` re-exercised against the migrated,
   fixed `create_app()`, now including `X-Client-Id` on every call.
5. **Recruitment Agent non-regression**: `hr-recruitment-common`'s and the
   Recruitment Agent's own test suites pass unmodified, confirming zero
   effect on that repo.
6. **Dependency isolation check**: `hr-onboarding-common` has no
   `hr_recruitment_common` in its dependency closure.
7. **Security regression suite (new)**: every test listed in the right-hand
   column of C.3 passes — this is the gate that confirms S1–S6 are not
   silently present in the shipped fork.

---

## Part D — Final checklists

### D.1 Pre-existing vulnerabilities vs. migration defects (explicit distinction)

- **Pre-existing vulnerabilities (S1–S6, Part B.2):** exist in the code
  today, confirmed by direct citation, independent of this migration. They
  must be fixed as part of what "Phase 3/4 complete" means (Part B.4,
  C.1 step 5), not deferred, and not re-shipped unchanged under the excuse
  that "the fork just mirrors the original."
- **Migration defect risks (process risk, not current vulnerabilities):**
  mistakes the fork/migration process itself could introduce if done
  carelessly — distinct in kind, not severity, from S1–S6:
  - An import path missed during repointing (caught by C.4.1 — the test
    suite simply fails to collect/import).
  - The `Scope`/`Purpose` rename applied in the library but missed in
    `tests/conftest.py` (caught by C.4.1).
  - `langgraph`/`langchain-core` dropped from both pyproject files
    simultaneously, breaking `agent.py`'s import (caught by C.4.1/C.4.6).
  - A security fix (C.3) implemented in the library but not wired up on the
    agent-repo side (e.g., `tenant_check` hook added to `create_app()` but
    never passed from `onboarding_agent/api.py`) — caught only by the
    specific regression tests in C.3, which is why each fix has one.

### D.2 Required authentication boundary (restated from B.5)

A trusted gateway/IdP must sit in front of this service, validate identity
by a real mechanism, and set `X-User-Id`/`X-User-Roles`/`X-Client-Id`
itself (stripping client-supplied copies) before this app ever sees a
request. **This does not exist today** and is not created by this design.
It is a named, explicit deployment blocker — not a loose end this document
is pretending is handled.

### D.3 Acceptance criteria — required before `hr-onboarding-common` can be
considered independently packaged and safe to use

1. Package builds and installs standalone with zero `hr_recruitment_common`
   in its dependency closure (C.4.6).
2. `hr-recruitment-common` and the Recruitment Agent are unmodified and
   their own test suites still pass (C.4.5).
3. All pre-existing structural trims (Part A.3) ported with zero
   unexplained behavior change — existing test suite green except for the
   enumerated breaking-change deltas (C.3).
4. **All six security findings (S1–S6) have their corresponding fix from
   Part B.4 implemented in the fork**, and **every regression test in
   C.3's right-hand column passes**, specifically including:
   - Cross-tenant approval list/get/decide is rejected (S1).
   - Cross-tenant `/v1/runs` execution is rejected (S4).
   - The kill switch blocks registry-backed tool calls, not only the
     LangGraph path (S6).
   - A failed post-approval action is surfaced (not silently swallowed)
     and is retryable without fabricating a new approval (S5).
5. The agent-repo-side fixes are applied and tested: `checklist.verify_signature`
   is role-gated or HITL-gated (S3), and the `tenant_check` hook is wired
   into `onboarding_agent/api.py`'s `create_app(...)` call (S4).
6. The required authentication boundary (D.2) is **documented in the new
   library's README as a deployment prerequisite**, with the existing
   `TODO(identity)`-style caveat preserved and strengthened, not weakened
   or removed — closing it fully (a real IdP integration) is explicitly
   out of scope for this migration and must not be claimed as done.
7. No empty placeholder modules exist; every excluded component (Part A.1)
   is absent, not stubbed.
8. This document itself reflects the as-built state after implementation —
   any deviation discovered during Phase 3/4 is reported back, not silently
   reconciled.

**None of the above has been done. This is the acceptance bar for when it
is.**

---

## Summary for review

- Package structure, directory tree, and module list: **unchanged** from the
  originally approved design.
- Six security findings (S1–S6) were investigated against the real code
  (file:line evidence throughout Part B) and confirmed as **pre-existing,
  not migration-introduced**. Four require fixes that can only live in the
  fork (since `hr-recruitment-common` must stay untouched); one requires an
  agent-repo fix; one (RBAC) requires both plus an infrastructure piece
  (a real identity gateway) that this document explicitly does not
  fabricate or claim exists.
- Concrete, still-unimplemented fix designs are specified for each finding,
  each mapped to a required regression test (Part C.3) and folded into the
  migration sequence (C.1) and acceptance criteria (D.3) as mandatory, not
  optional, parts of "complete."
- `hr-recruitment-common` and the Recruitment Agent remain untouched by
  this design and by every step of the proposed migration sequence.

**No implementation has begun. Awaiting approval before starting Phase 3.**
