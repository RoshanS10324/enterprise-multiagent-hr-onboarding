# brain

Instructions and persona prompts for the agent.

| File | What it is |
|---|---|
| [persona.md](persona.md) | Who the agent is, the six requirements it serves (R1-R6), and the four things it never does. Prepended to every task. |
| [tasks/](tasks/) | One YAML per task the agent may ask a model to do. |

## Tasks

| Task | Authority | Sees |
|---|---|---|
| [intake_intent](tasks/intake_intent.yaml) | `ADVISORY_ESCALATE_ONLY` | the email subject line, nothing else |
| [discrepancy_summary](tasks/discrepancy_summary.yaml) | `DRAFT_FOR_HUMAN` | field names and source names, no values |
| [duplicate_explanation](tasks/duplicate_explanation.yaml) | `DRAFT_FOR_HUMAN` | which signals matched, not what they matched on |

## The two authority levels

`ADVISORY_ESCALATE_ONLY` — the response may send something to a human.
It may never clear a review the deterministic rules raised, and it can
never be the reason a case opens. Being wrong costs an unnecessary
review.

`DRAFT_FOR_HUMAN` — produces text for a person to read. It never
branches control flow and never changes a case.

There is no third level. UC-01 makes no decision with a model.

## Why `inputs_allowed` matters

Each task declares the inputs it may receive, and
[`llm/guard.py`](../src/sub_agent/llm/guard.py) refuses anything else
before a prompt is assembled. Salary cannot reach a model because no
task declares it — passing it raises `LlmInputRefused` rather than
relying on a regex to catch it. A task file that *did* declare
`salary_and_terms` would fail to load at all.

Note what is absent from `intake_intent`: the email **body**. UC-01
never reads it, which is what removes it as a prompt-injection surface.

## Scope

The persona states the mapped business requirements verbatim from
[specs/uc01-requirements.yaml](../specs/uc01-requirements.yaml), which
is the authoritative statement of what UC-01 covers. A test asserts
every requirement id and the expected output appear in the persona, so
the agent's stated job cannot drift from the owner's.

## Changing these files

These are agent behaviour, so changes need tests or eval cases, same as
code (CLAUDE.md). [tests/test_llm.py](../tests/test_llm.py) validates
every file here: required keys, declared authority, that `task` matches
the filename, that no forbidden input is declared, and that every label
the fake client can emit is one the task declared.
