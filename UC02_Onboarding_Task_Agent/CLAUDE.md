# CLAUDE.md — UC-02 Onboarding Task Management (`logesh`)

Instruction file for AI coding sessions on this branch.
**Detailed verified context: [`docs/UC02_CONTEXT.md`](docs/UC02_CONTEXT.md).** Read it before
planning work; do not duplicate its content here.

## What this branch is

This branch contains **UC-02 — Onboarding Task Management** only: a Phase 1
**local synthetic-data prototype** that plans and tracks onboarding tasks after an
offer is accepted. Implementation lives in `src/onboarding_task_management/`.

It is **not** production ready. There is no FastAPI app, no LangGraph graph, no
orchestrator, no HRIS/ITSM/e-sign/email connector, no LLM call, and no integration with
the `hr-recruitment-common` shared harness.

## Verify before you claim

The root folder tree mirrors the official Enterprise HR Agent layout, but **most root directories are
empty `.gitkeep` placeholders**, not working code. Likewise, earlier conversation
summaries are not authoritative.

Before stating that any capability exists:

1. Open the actual file. Do not infer behaviour from a directory name, a docstring, or a
   prior summary.
2. Prefer `rg`/`grep` plus reading the code over assuming.
3. Run the tests (below) rather than asserting they pass.
4. If something is unverified, say so explicitly. Never invent integrations, progress or
   test results.

## Tests

```
python -m unittest discover -s tests -t .
```

`pytest` also works. Expected: **257 tests, 0 failures**. Report real output; if a run
fails, say so with the output.

## Preserve the existing implementation

Do not rewrite or replace working UC-02 code. Specifically, do not modify without an
explicit request:

- `src/onboarding_task_management/**` — the implementation
- `tests/**` — the suite (257 tests)
- `README.md`, `pyproject.toml`, `guardrails/policy.yaml`
- `docs/pre-development-review.md` — historical pre-implementation analysis

Keep the six empty `src/*.py` legacy placeholders in place until deletion is approved.
They shadow top-level module names (`import config` resolves to the empty
`src/config.py`), so always import package-qualified:
`from onboarding_task_management import config`.

## Engineering rules

1. Synthetic data only. No real employee data. Contracts carry no name/email/contact field.
2. Audit entries carry IDs, roles, counts and outcomes only — never record contents.
3. Deterministic behaviour: derived IDs, explicit `as_of` dates, no clock reads in
   business logic, no randomness, no network.
4. Gates, autonomy and thresholds belong in `guardrails/policy.yaml`. If a value must be
   mirrored in Python, keep `tests/test_policy_parity.py` passing.
5. No provider SDKs, no HTTP clients, no transport. `reminders.py` produces drafts and
   must stay transport-free.
6. Never auto-act on a hard-gated item; an approval is valid only for the gate's own
   operation and only while the onboarding is active.

## Git safety

- Work only on `logesh`. Do not create branches.
- Never check out or modify `main`.
- Never `git merge origin/Roshan` — unrelated histories; it is a structure reference only.
- Never copy UC-03 or UC-04 implementation code (`src/agents/`, `src/api/`, `src/state/`,
  `uc04-equipment-access/`, `brain/onboarding/`, `knowledge/checklists/`, UC-03 tests).
- No `reset --hard`, `clean`, or force-push. Do not delete files.
- **Do not commit or push without explicit approval.**
