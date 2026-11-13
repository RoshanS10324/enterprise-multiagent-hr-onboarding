# CLAUDE.md

Working rules for any AI assistant contributing to this repository.

## UC-01 scope

UC-01 "Employee Record Creation" covers:
- the start gate
- duplicate/variant check
- case creation using onboarding form version 1
- discrepancy flagging
- preparing the employee record in Zoho People/Payroll
- handling changes to employee details after case creation

Do not implement functionality outside this scope under the UC-01 label.

## Data and secrets

- Use synthetic joiner data only. Never real personal data, in code, tests,
  fixtures, or examples.
- No secrets in the repo. Configuration comes from `.env` locally and from
  Key Vault in deployed environments.

## Tooling boundaries

- Every call to a business system (HRIS, finance, Graph, IT service desk,
  documents) goes through a tool defined in `mcp/`. Stubs are acceptable for
  now, but nothing talks to a business system directly.
- Anything sent to a joiner, any write to a system of record, and any spend
  waits for a person's approval before it happens.
- Never calculate, round, or otherwise change salary figures. Salary values
  are copied exactly as approved, with no transformation.
- The approved onboarding form is the source of truth. Differences between
  it and other data are flagged to HR; they are never silently overwritten.
- Every action, decision, and approval is written to the audit log.
- Salary, bank, and loan data stay inside HR and payroll scopes. They are
  not exposed to other scopes or tools.

## Changing agent behavior

Changes to `brain/`, `policy/`, `skills/`, or `knowledge/` change the
agent's behavior and require tests or eval cases, the same as a code
change.
