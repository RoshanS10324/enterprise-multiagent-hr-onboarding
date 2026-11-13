# UC-01 evals

UC-01 has no AI step: every decision is deterministic Python (policy
rules, state machines, field-for-field checks). So these are not
model-graded evals - they are deterministic, end-to-end golden scenarios
run through `CaseService` (the single real entry point), plus
`open_onboarding_case` for the one step before a case exists.

Each scenario in `scenarios/*.json` describes a sequence of actions by
named viewer roles (synthetic users only - no real personal data) and the
expected outcome of each step, plus final assertions about the resulting
state (case stage, published events, gateway write count, audit actions
present, and that no invariant is violated). `runner.py` plays each
scenario against a fresh set of in-memory adapters, checks every
expectation, and - on every scenario regardless of what it's testing -
confirms that no audit entry, event, or approval contains the scenario's
synthetic salary text, email, or candidate name.

Run directly:

```
uv run python evaluation/run_evals.py
```

This writes `evaluation/uc01/results.json` (per-scenario pass/fail and a
coverage list of the framework ids - EC-xx, AC-xx, DL-xx, T-xx - the
scenarios touch) and exits 1 if any scenario fails. `tests/test_evals.py`
runs the same runner in-process so `uv run pytest` covers it too.

If an AI step is ever introduced into UC-01 (e.g. an LLM-assisted
decision), model-based evaluators (via the Foundry SDK) would be added
alongside these - not instead of them, since the deterministic parts stay
deterministic.
