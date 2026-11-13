# context_layer/skills

What the agent is allowed to *see* when it runs each skill.

`skills/` (repo root) answers "what can the agent do" -- inputs,
outputs, when to use each skill. This directory answers the narrower
question: for that skill, which fields, records, and tools are in scope,
and which are deliberately withheld.

The two are separate on purpose. A skill definition changing is a
capability change; a context definition changing is a data-exposure
change, and the guardrail that matters most here --

> Salary, bank, and loan data stay inside HR and payroll scopes. They
> are not exposed to other scopes or tools.

-- is a statement about context, not about capability.

## The files

One YAML file per skill, named after the skill. [`_schema.yaml`](_schema.yaml)
defines the required keys.

| Skill | Sees salary | Writes |
|---|---|---|
| [start_case](start_case.yaml) | no | no |
| [compare_sources](compare_sources.yaml) | no | no |
| [change_employee_details](change_employee_details.yaml) | no | no |
| [prepare_employee_record](prepare_employee_record.yaml) | no | no |
| [write_employee_record](write_employee_record.yaml) | **yes**, at write time only | **yes** |
| [case_status](case_status.yaml) | no | no |

One skill of six reads a salary value, at the single moment it is
written. That is the shape to preserve.

## Rules

- Scope is deny by default: if a field is not in `in_scope`, the skill
  does not see it.
- Widening a scope is a behaviour change and needs tests or eval cases,
  the same as a code change (CLAUDE.md, "Changing agent behavior").
- The runtime enforcement points are named in each file's
  `enforced_by`. Files here declare intent and must agree with the
  code; a declaration without matching enforcement is a gap, not a
  guarantee.

## What is checked automatically

[`tests/testcase.py`](../../tests/testcase.py) validates, for every file
here:

- the schema keys are present and `skill` matches the filename;
- every field name is a real `OnboardingForm` field, and `in_scope` and
  `excluded` together cover all of them without overlapping;
- every role is a real `Role` enum member;
- every `enforced_by` path exists;
- `salary_and_terms` is excluded everywhere except
  `write_employee_record`.

That last check is the load-bearing one: adding salary to another
skill's context fails the suite rather than slipping through review.

## Known gap

Bank and loan data are named in the guardrail but are not yet modelled
on `OnboardingForm` -- today the only sensitive field is
`salary_and_terms`. The exclusions here will need revisiting when those
fields exist; see [docs/known-gaps.md](../../docs/known-gaps.md).

Declared guardrails live in [guardrails/policy.yaml](../../guardrails/policy.yaml);
authorization (who may act, not what is visible) lives in
[policy/uc01_policy.toml](../../policy/uc01_policy.toml).
