# Common Library Analysis — `hr-recruitment-common` vs. a proposed `hr-onboarding-common`

> **Note (2026-10-27, agent v0.3.0):** this is a historical analysis. Its paths refer to the
> agent's earlier `src/onboarding_agent/` layout (now `src/sub_agent/`, `connectors/`,
> `context_layer/`; `README.md` maps old locations to new ones). The agent now depends on
> `hr-onboarding-common`, not `hr-recruitment-common`.

**Status: Phase 1 (read-only analysis) only. No code has been moved, copied, or modified. Do not
begin Phase 2 (design) until the open decision in §8 is resolved.**

Grounded in: a fresh directory/line-count/docstring audit of
`dact-core-functions-hr-sample-reference/hr-recruitment-common`, and an exhaustive grep of every
`hr_recruitment_common` import across `HR-Onboarding-Agent/src` and `HR-Onboarding-Agent/tests`
(27 files touch it; the exact import lines were enumerated, not sampled).

---

## 1. Existing common-library architecture

`hr-recruitment-common` is a single `uv` workspace package
(`dact-core-functions-hr-sample-reference/hr-recruitment-common`, `src/hr_recruitment_common/`)
that every recruitment agent in that repo depends on. Its own `README.md` describes it as "shared
harness for the HR recruitment agents," but the actual code splits cleanly into two tiers:

- **A domain-agnostic agent harness** — request/response contracts, the LangGraph-based
  `BaseAgent`/`RunContext` engine, the LiteLLM gateway, the FastAPI service shell, policy
  enforcement, HITL approvals, audit logging, observability, guardrail primitives, and a generic
  BM25 knowledge index. None of this code mentions candidates, requisitions, or resumes in its
  logic — only in docstrings and a few default enum values.
- **Recruitment-specific business modules** — the ATS connector (`Requisition`/`Candidate`/
  `Application`), the technology skills ontology, synthetic UAE hiring data, and test scaffolding
  wired to recruitment fixtures.

This split is not formalized as two packages today — it is one package, and the line between the
two tiers is a matter of which modules happen to reference recruitment vocabulary, not a package
boundary.

### 1.1 Directory tree

```
hr-recruitment-common/
├── pyproject.toml
├── README.md
└── src/hr_recruitment_common/
    ├── __init__.py
    ├── agent.py                      # BaseAgent, AgentContext, RunContext, run_context()
    ├── api.py                        # create_app(): shared FastAPI service shell
    ├── config.py                     # Settings, get_settings(), resolve_agent_home()
    ├── contracts.py                  # AgentRequest/Response, Actor, Scope, Proposal, Purpose, ...
    ├── synthetic.py                  # Recruitment-only: synthetic UAE hiring world
    ├── testing.py                    # Recruitment-only: SampleAgent, RECRUITER actor, make_request
    ├── connectors/
    │   └── ats/
    │       ├── base.py               # Recruitment-only: ATSPort, Requisition, Candidate, Application
    │       └── mock.py               # Recruitment-only: MockATS
    ├── context_layer/
    │   ├── __init__.py               # Brain (ontology + knowledge + memory)
    │   ├── knowledge.py               # KnowledgeIndex (generic BM25, tier-filtered)
    │   ├── memory.py                  # EpisodicMemory (generic event store, PII-scrubbed)
    │   ├── ontology.py                # Recruitment-only: skills/roles/panel taxonomy
    │   └── data/
    │       ├── ontology/technology/*.yaml    # Recruitment-only taxonomy data
    │       ├── skills/untrusted-doc-handling/SKILL.md   # Generic prompt-composition skill
    │       └── static/{company,policies}/*.md           # Recruitment sample policy docs
    ├── llm/
    │   ├── base.py                    # ModelTier, LLMClient, BaseLLM, FakeLLM, error types
    │   ├── callbacks.py                # LLMEvent, LLMCallback
    │   └── gateway.py                  # LiteLLMGateway (retries, fallback chain)
    ├── observability/
    │   ├── logging.py                  # JSON logging, configure_logging()
    │   └── tracing.py                  # OpenTelemetry configure_tracing(), span()
    └── platform/
        ├── audit.py                    # AuditLog (JSONL)
        ├── fairness.py                 # Recruitment-only: four-fifths adverse-impact check
        ├── guardrails.py                # Injection/PII/grounding primitives (generic engine,
        │                                #   recruitment-flavored pattern lists)
        ├── hitl.py                      # HITLInbox, ApprovalItem, Decision, ReasonCode
        ├── policy.py                     # Policy, PolicyStore, PolicyViolation, GateDef
        ├── skills.py                     # SkillLibrary (SKILL.md prompt composition)
        └── tools.py                      # ToolGateway (allowlist + approval + audit wrapper)
```

### 1.2 Module responsibilities and classification

| Module | Lines | Classification | What it actually does |
|---|---:|---|---|
| `agent.py` | 198 | **Generic engine** | `BaseAgent` (kill switch, policy load, exception mapping), `AgentContext` (settings/llm/policies/audit/hitl/brain/skills/ats), `RunContext` (the `rc.llm`/`rc.tools`/`rc.propose` facade every graph node uses) |
| `api.py` | 145 | **Generic shell** | `create_app()` — the FastAPI routes (`/healthz`, `/v1/runs`, `/v1/approvals*`) every agent service exposes; `default_context()` (recruitment-coupled: hard-requires `ATS_ADAPTER`) |
| `config.py` | 77 | **Generic** | `Settings` dataclass from env vars, `resolve_agent_home()` |
| `contracts.py` | 156 | **Generic envelope, hiring-flavored defaults** | `AgentRequest`/`AgentResponse`/`Proposal`/`Provenance`/`ExceptionCode`; `Purpose` enum ships `HIRING` (we added `ONBOARDING` to it in this engagement); `Scope.requisition_ids` is a recruitment-named field reused generically |
| `llm/base.py`, `llm/gateway.py`, `llm/callbacks.py` | 302 | **Fully generic** | `ModelTier`, `LLMClient` protocol, `FakeLLM`, `LiteLLMGateway` (provider-neutral retries/fallback/structured output) |
| `observability/logging.py`, `observability/tracing.py` | 96 | **Generic** | JSON logging with trace ids, OpenTelemetry spans |
| `platform/audit.py` | 45 | **Fully generic** | Append-only JSONL audit log — our single most-imported symbol (18 files) |
| `platform/guardrails.py` | 200 | **Generic engine, recruitment-flavored data** | `detect_injection`, `wrap_untrusted`, `redact_pii`, `quote_is_grounded`, `detect_pay_figures`, `scan_protected_terms` — the *functions* are domain-agnostic text processing; the *pattern lists* inside them reference candidates, shortlisting, bands, nationality |
| `platform/hitl.py` | 168 | **Generic, one recruitment tell** | `HITLInbox`, `ApprovalItem`, `Decision`; `ReasonCode` includes `WRONG_BAND` (recruitment-specific) alongside generic codes |
| `platform/policy.py` | 100 | **Fully generic** | `Policy`/`PolicyStore`/`PolicyViolation` — policy.yaml loading and enforcement, with zero recruitment vocabulary |
| `platform/skills.py` | 46 | **Fully generic** | `SkillLibrary` — `SKILL.md` prompt-composition loader (not the same thing as the recruitment skills *ontology*) |
| `platform/tools.py` | 47 | **Fully generic** | `ToolGateway` — allowlist/approval/audit wrapper around any connector call |
| `context_layer/knowledge.py` | 85 | **Fully generic** | BM25 keyword retrieval over tier-labeled markdown chunks |
| `context_layer/memory.py` | 59 | **Generic store, HR-flavored scrub list** | `EpisodicMemory`; `PROTECTED_KEYS` includes `emirates_id`/`passport`/`nationality` |
| `connectors/ats/*` | 154 | **Recruitment-only** | `ATSPort`, `Requisition`, `Candidate`, `Application`, `MockATS` |
| `context_layer/ontology.py` + data | 144 + yaml | **Recruitment-only** | Technology skills/role/panel taxonomy |
| `synthetic.py` | 210 | **Recruitment-only** | Synthetic UAE hiring world generator |
| `testing.py` | 84 | **Recruitment-only** | `SampleAgent`, `RECRUITER` actor, `make_request()` wired to `requisition_id` |
| `platform/fairness.py` | 35 | **Recruitment-only (by application, generic code)** | Four-fifths adverse-impact statistic — a hiring-law concept, not needed by onboarding |

### 1.3 `pyproject.toml` (dependencies the harness pulls in)

```
litellm>=1.83.0 (never 1.82.7/1.82.8 — pinned in a code comment, easy to lose on a fork)
langgraph>=0.4.0, langchain-core>=0.3.0, pydantic>=2.10.0, pyyaml>=6.0, python-dotenv>=1.0.0,
tenacity>=9.0.0, httpx>=0.28.0, opentelemetry-api/sdk>=1.20.0, fastapi>=0.115, uvicorn[standard]>=0.30,
tzdata>=2024.1
```
Build: hatchling. No MCP client/server dependency anywhere in this list or the codebase — see §4.

---

## 2. Dependency and import map (Onboarding Agent → `hr_recruitment_common`)

27 files in `HR-Onboarding-Agent` reference `hr_recruitment_common` (26 with an actual `import`
statement; 1 — `calendar/base.py` — only in a docstring comment). Consolidated by submodule:

| Submodule | Names imported | Breadth |
|---|---|---|
| `hr_recruitment_common.agent` | `AgentContext`, `BaseAgent`, `run_context` | 3 files |
| `hr_recruitment_common.api` | `create_app`, `ApprovalRef` | 1 file |
| `hr_recruitment_common.config` | `get_settings`, `resolve_agent_home` | 2 files |
| `hr_recruitment_common.contracts` | `Actor`, `Purpose`, `ExceptionCode`, `ResponseStatus`, `new_id`, `now_utc`, `AgentRequest`, `Scope`, `WorkflowRef`, `Autonomy` | 13 files (`Actor`/`Purpose` alone: 10) |
| `hr_recruitment_common.llm` | `LLMClient`, `LiteLLMGateway`, `FakeLLM`, `extract_input_block`, `ModelTier`, `input_block`, `LLMError` | 8 files |
| `hr_recruitment_common.context_layer.knowledge` | `KnowledgeIndex` | 1 file |
| `hr_recruitment_common.platform.audit` | `AuditLog` | **18 files** — the single most-depended-on symbol in the whole codebase |
| `hr_recruitment_common.platform.guardrails` | `detect_injection`, `quote_is_grounded`, `wrap_untrusted`, `detect_pay_figures`, `scan_protected_terms` | 3 files |
| `hr_recruitment_common.platform.hitl` | `HITLInbox`, `ApprovalItem`, `Decision`, `ReasonCode` | 13 files |
| `hr_recruitment_common.platform.policy` | `Policy`, `PolicyStore`, `PolicyViolation` | 8 files |
| `hr_recruitment_common.platform.tools` | `ToolGateway` | 1 file |

**Confirmed by direct grep — zero imports anywhere in this repo** of:
`connectors.ats`, `context_layer.ontology`, `synthetic`, `testing`, `platform.fairness`.

This is the single most important fact this analysis turned up: **the onboarding agent already
depends on exactly the domain-agnostic tier of `hr-recruitment-common`, and nothing else.** The
separation of concerns the task asks for already exists at the *import* level; what doesn't exist
is a *package* boundary matching it.

---

## 3. Reusable components (candidates for a new library, if one is built)

Every module in the consolidated table above: `agent.py`, `api.py`, `config.py`, `contracts.py`
(the envelope, not the `HIRING`/`WRONG_BAND` defaults), the whole `llm/` package, `context_layer/
knowledge.py`, and `platform/{audit,guardrails,hitl,policy,skills,tools}.py`. `observability/*` is
not imported directly by our code but is called internally by `create_app()`'s lifespan
(`configure_logging`, `configure_tracing`), so it is a transitive requirement of `api.py`, not an
optional one.

Sum: **≈1,665 lines** of code across 13 modules. This is not a handful of small utilities — it is
essentially the entire operational harness (agent engine, retrying LLM gateway, approval system,
audit, guardrails, policy enforcement, tracing).

## 4. Recruitment-specific components that must not be copied

`connectors/ats/*`, `context_layer/ontology.py` (+ its bundled YAML data), `synthetic.py`,
`testing.py`, `platform/fairness.py`. Confirmed zero usage by this repo. Also do not copy, without
deliberately rewriting first: the `HIRING`/`WRONG_BAND` enum *members* (the enums themselves are
fine to keep, generically), the candidate/shortlisting-flavored pattern lists inside
`guardrails.py`, or `testing.py`'s `RECRUITER`/`SampleAgent` fixtures — we already wrote our own
onboarding-flavored equivalents (`tests/conftest.py`) rather than reusing those, for exactly this
reason.

**On MCP specifically** (task checklist item 7): there is no MCP client, server, or tool-registry
concept anywhere in `hr-recruitment-common`. "Tool registry" in this codebase means `ToolGateway`
(a policy-allowlist/approval/audit wrapper around a plain Python callable) — not an MCP tool
registry. Our own `OnboardingToolRegistry` (`tool_registry.py`) is *new* orchestration code we
wrote on top of `ToolGateway`, not a port of anything that exists in `hr-recruitment-common` —
each recruitment agent instead calls `rc.tools.call(...)` directly, inline, inside its own
LangGraph nodes, because no single recruitment agent has enough distinct operations to justify a
separate registry class. There is nothing to "migrate" here; `OnboardingToolRegistry` is
onboarding-specific orchestration and belongs with the agent regardless of where the rest of the
harness ends up.

## 5. Components that are mocked, incomplete, or absent

- **No MCP integration** at all (see §4) — if the business requirement needs one, it has to be
  designed from scratch; there is no existing pattern to adapt.
- **`observability/tracing.py`** has a standing `# TODO(azure-monitor): azure-monitor-opentelemetry-exporter`
  — tracing is configured but has no real export destination wired up yet, for either agent.
- **Incidental, currently-harmless coupling we discovered while tracing imports:**
  `AgentContext.build()` *unconditionally* constructs `Brain.load(settings.context_dir, ...)`
  internally, which in turn builds the recruitment `Ontology` from
  `hr_recruitment_common`'s own bundled `context_layer/data/ontology/technology/*.yaml` — every
  time our `build_context()` runs. We never read `ctx.brain` anywhere (confirmed: zero
  `context_layer.ontology` imports), so this has no functional effect on the onboarding agent
  today — but it does mean every process start silently parses and holds recruitment taxonomy
  data in memory for no reason. This is also exactly why Phase 5 of this project built a separate
  `PolicyKnowledgeBase` instead of using `ctx.brain.knowledge`: the shared `KnowledgeIndex` reads
  from one single global directory (`Settings.context_dir` is not configurable per-agent), which
  cannot support tenant-scoped policy documents.
- **`default_context()` in `api.py`** hard-requires an `ATS_ADAPTER` env var — this is why
  `onboarding_agent/api.py` already builds its own `AgentContext` and passes it to `create_app(context=...)`
  instead of using the shared default-context path.

---

## 6. Risks of extracting a separate `hr-onboarding-common`

1. **Duplication, not separation.** Because the onboarding agent already only touches the
   domain-agnostic tier, building a separate library means copying (not re-architecting) ~1,665
   lines of already-shared, already-generic infrastructure — the agent engine, the retrying LLM
   gateway, `ToolGateway`, `HITLInbox`, `AuditLog`, policy enforcement, and the BM25 knowledge
   index — into a second codebase.
2. **Ongoing double maintenance.** Any future fix to `LiteLLMGateway`'s retry/fallback logic, a
   security patch to `ToolGateway`'s approval check, a `HITLInbox` concurrency fix, etc. would need
   to be written and tested twice, in two independent repos, with no mechanism keeping them in
   sync. The litellm version pin (`never 1.82.7/1.82.8`) is a concrete example of a constraint
   that is easy to silently lose on a fork.
3. **The duplication can't stop at the "generic" modules.** `create_app()` constructs
   `agent_cls(ctx)` and expects `ctx`/`agent` to match `hr_recruitment_common.agent.BaseAgent`'s
   shape. To be genuinely independent (not just independently *named*), a fork would also need to
   duplicate `api.py` itself — meaning its own FastAPI/uvicorn/starlette version management too,
   not just the agent engine.
4. **Dependency duplication.** `hr-onboarding-common` would need its own `pyproject.toml`
   re-declaring litellm/langgraph/pydantic/fastapi/etc., with its own version constraints to keep
   in sync by hand.
5. **Workspace placement risk.** The user's proposed destination —
   `dact-core-functions-hr-sample-reference/hr-onboarding-common/` — sits *inside* the recruitment
   monorepo's own uv workspace. Placing it there would tie the new library's lockfile, CI, and
   release cadence to the recruitment repo it's explicitly meant to be independent from, and would
   require editing that repo's root `pyproject.toml` `[tool.uv.workspace] members` list — arguably
   a "modification to the recruitment repository," which conflicts with the instruction not to
   touch it. **Recommendation: place it as a sibling directory, `D:\Projects\D-ACT\hr-onboarding-common\`**,
   exactly parallel to how `HR-Onboarding-Agent` itself was deliberately kept out of that
   workspace in this project's very first phase, for the same reason.
6. **A cleaner fix exists but is out of scope here.** The architecturally "correct" resolution —
   extracting the already-generic tier out of `hr-recruitment-common` into a neutrally-named
   shared package (e.g. `dact-agent-common`) that *both* recruitment and onboarding depend on,
   leaving a slim `hr-recruitment-common` with only the ATS connector, ontology, synthetic data,
   and test fixtures — would eliminate the duplication risk entirely. This is explicitly out of
   bounds for this task ("do not modify the original recruitment library"), but is worth raising
   with whoever owns the `dact-core-functions-hr-sample-reference` repo as a longer-term option.

---

## 7. What Phase 2 would actually build, under the instructed approach

If you confirm proceeding with a dedicated library (Option A below), Phase 2 would design
`hr-onboarding-common` as a near-verbatim restructuring of the 13 generic modules in §3 — same
package conventions (`src/hr_onboarding_common/`, hatchling build, the same `agent.py`/`contracts.py`/
`llm/`/`platform/` shape) — re-homed under a neutral package name, with the `HIRING`/`WRONG_BAND`
enum members dropped (onboarding already added `Purpose.ONBOARDING` to the *shared* contracts in
an earlier phase of this project; a forked copy would need its own enum values from scratch,
independent of that prior change) and the recruitment-flavored guardrail pattern lists reviewed
line-by-line for what's actually still relevant (prompt-injection detection: yes; candidate/
shortlisting pay-figure wording: needs rewording, not blind copying).

---

## 8. Decision needed before Phase 2 starts

Given the risk in §6.1–§6.4, please confirm which path to take:

- **Option A — build `hr-onboarding-common` as instructed.** Full, independently-maintained fork
  of the ~1,665-line generic tier, placed at `D:\Projects\D-ACT\hr-onboarding-common\` (sibling,
  not inside the recruitment monorepo). Accepts the double-maintenance risk in exchange for true
  packaging independence.
- **Option B — keep the status quo.** No new library; continue depending on
  `hr-recruitment-common` for the already-generic tier (confirmed, in §2, to be 100% free of
  recruitment-specific imports today). Zero duplication, zero drift risk, but the dependency's
  *name* remains "recruitment" even though nothing recruitment-specific is actually used.
- **Option C — flag it upstream instead.** Don't fork anything in this project; write up §6.6's
  recommendation as a proposal for the `dact-core-functions-hr-sample-reference` repo owner to
  extract a neutrally-named shared package later, benefiting both agents without duplication.

I have not started Phase 2 (design) or any implementation. Tell me which option to proceed with,
or if you want to proceed with Option A regardless of the duplication trade-off, say so explicitly
and I will continue with the design phase on that basis.
