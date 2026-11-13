# specs

Technical specifications.

[uc01-requirements.yaml](uc01-requirements.yaml) is the authoritative
statement of UC-01's scope: the six mapped business requirements
(R1-R6) and the expected output, in the use-case owner's own wording.

Everything else points back at it:

- [brain/persona.md](../brain/persona.md) states them, so the agent's
  idea of its job matches the owner's.
- [skills/](../skills/) declares which requirements each skill serves.
- [scripts/verify_requirements.py](../scripts/verify_requirements.py)
  walks a real case from acceptance email to written record and checks
  each requirement against actual behaviour. CI runs it. Add `--verbose`
  for the full step-by-step trace of inputs, rules, approvals, state
  changes and audit events behind each check.

If the implementation and this file disagree, the implementation is
what moves.
