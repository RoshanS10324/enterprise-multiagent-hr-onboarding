# skills

What the agent can do: inputs, outputs, and when to use each skill.

One YAML per skill; [`_schema.yaml`](_schema.yaml) defines the required
keys. Each has a matching context file in
[context_layer/skills/](../context_layer/skills/) saying what it may
*see* while doing it.

| Skill | Serves | Approval it waits on | Uses a model |
|---|---|---|---|
| [start_case](start_case.yaml) | R1, R2, R5 | HC_12_VARIANT, if a variant or duplicate is flagged | `duplicate_explanation` (draft only) |
| [compare_sources](compare_sources.yaml) | R6 | HC_04_DATA | `discrepancy_summary` (draft only) |
| [change_employee_details](change_employee_details.yaml) | R3, R5 | HC_01_FORM | no |
| [prepare_employee_record](prepare_employee_record.yaml) | R3, R4 | HC_05_HRIS_WRITE | no |
| [write_employee_record](write_employee_record.yaml) | R4 | HC_05_HRIS_WRITE | no |
| [case_status](case_status.yaml) | R3 | none (read-only) | no |

`R1`–`R6` are the mapped business requirements in
[specs/uc01-requirements.yaml](../specs/uc01-requirements.yaml). Every
one is served by at least one skill, and
[scripts/verify_requirements.py](../scripts/verify_requirements.py)
walks a real case end to end and checks each of them against actual
behaviour. CI runs it.

Two things to read off that table. Every skill that changes anything
waits on a named human checkpoint. And no skill that writes to a system
of record goes near a model — the two tasks that exist only draft text
for a person.

`intake_intent` is the third brain task and belongs to no skill: it runs
at the trigger boundary, before a case exists. It can send an email to
HR review; it can never be why a case opens.

## Changing these files

Skill definitions are agent behaviour, so changes need tests or eval
cases, same as code (CLAUDE.md). [tests/test_llm.py](../tests/test_llm.py)
checks every file here against `_schema.yaml`, that each has a matching
context file, that `entry_point` resolves to something that really
exists, that `approval` names a real `Checkpoint`, that every
`llm_tasks` entry is a real task in [brain/tasks/](../brain/tasks/), and
that no requirement is left without a skill to serve it.
