# hr-onboarding-common

Shared harness for the HR Onboarding Agent: contracts, LiteLLM gateway, guardrails, policy
enforcement, human-in-the-loop approvals, audit logging, observability, and knowledge retrieval.

This is an **independently packaged fork** of `hr-recruitment-common`
(`../dact-core-functions-hr-sample-reference/hr-recruitment-common`), not a dependency on it and
not a drop-in replacement for it. It was created so the HR Onboarding Agent and the Recruitment
Agent can evolve independently, and so the security fixes below could be made without touching the
Recruitment Agent's dependency graph. See
`../HR-Onboarding-Agent/docs/ONBOARDING_COMMON_ARCHITECTURE.md` for the full design rationale,
module-by-module mapping, and migration history.

## What's intentionally different from `hr_recruitment_common`

- No `Brain`, `Ontology`, `EpisodicMemory`, or `SkillLibrary` -- all had zero call sites in the
  onboarding codebase and are dropped, not stubbed.
- `Purpose` carries only `ONBOARDING`.
- `Scope.requisition_ids` is renamed to `Scope.subject_ids`.
- `Actor` gains `client_id` -- the single source of tenant identity threaded through contracts,
  `ApprovalItem`, and every tenant-scoped check.
- `ApprovalItem` requires `client_id` and carries `apply_error`.
- `HITLInbox.get`/`.list`/`.decide` are tenant-scoped at the query level, not by post-filtering.
- `HITLInbox.decide` no longer lets a follow-up-action failure masquerade as success or strand the
  item permanently; `retry_apply` recovers it.
- `ToolGateway.call` enforces the policy kill switch (`policy.enabled`), not only `BaseAgent.run`.
- New `platform/identity.py`: an `IdentityProvider` interface and `VerifiedActor` contract.
  `InsecureHeaderIdentityProvider` reproduces the old X-User-Id/X-User-Roles/X-Client-Id
  header-trust behaviour and is **disabled by default**.

## Required production authentication boundary

**No real identity-provider integration exists in this project.** `InsecureHeaderIdentityProvider`
performs no cryptographic or out-of-band verification -- it is a development/test adapter, and it
only runs at all when an application explicitly constructs it with `enabled=True`.

Before this library (or any application built on it) is exposed to any caller that is not fully
trusted, it must sit behind a gateway or reverse proxy that:

1. Authenticates the caller by a real mechanism (JWT validated against an IdP's JWKS, mTLS, or a
   server-validated session) -- never by trusting anything the client sends directly.
2. Derives `X-User-Id` / `X-User-Roles` / `X-Client-Id` from the *validated* token/session claims
   and sets them itself, stripping any client-supplied copies of the same headers.
3. Is the only network path to the application.

This is a deployment prerequisite, not something this library or the onboarding agent can satisfy
by itself. Until it exists, do not expose this service to untrusted callers.

## Development

```
uv sync
uv build
```

No `hr_recruitment_common` import should ever appear in this package.
