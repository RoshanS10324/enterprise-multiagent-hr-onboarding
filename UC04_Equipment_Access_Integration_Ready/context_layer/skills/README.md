# UC-04 context_layer/skills

Skill definitions for the UC-04 Equipment & Approved Access Management sub-agent. A skill is a named, read-only advisory capability that the Fake LLM advisory layer (`src/sub_agent/advisor.py`) applies to a case result. Skills never change a case, an item, an approval or a ticket, and they never call a tool.

## Skills in this folder

| File | Skill (`name`) | What it covers |
|---|---|---|
| `assess_case_and_requests.md` | `assess_case_and_requests` | Overall case status, start-gate conditions, item eligibility |
| `assess_readiness_risks_and_actions.md` | `assess_readiness_risks_and_actions` | Computed blockers and risks, mitigation proposals, follow-up actions |
| `explain_approvals_and_exceptions.md` | `explain_approvals_and_exceptions` | Pending or required approvals, designated approver roles, exception routes |
| `retrieve_access_equipment_requirements.md` | `retrieve_access_equipment_requirements` | Applicable access and equipment requirements from the client configuration |
| `recommend_role_standard_items.md` | `recommend_role_standard_items` | The role's standard equipment and access package from its configuration, recommended to HR when nothing has been requested (`NO_ITEMS_REQUESTED`); implemented in `src/sub_agent/role_packages.py` |
| `summarize_evidence_and_it_status.md` | `summarize_evidence_and_it_status` | Completion evidence, mock IT ticket progress, delivery and collection milestones |

A test (`tests/test_skills.py`) loads this folder, and `tests/test_knowledge_accuracy.py` checks that every code a skill names exists in the code.

## File format

Each skill is one Markdown file: YAML frontmatter between `---` lines, then a Markdown body.

| Frontmatter key | Meaning |
|---|---|
| `name`, `description` | Required. The skill id and a one-line summary |
| `use_case`, `version`, `type` | Default to `UC-04`, `1.0.0`, `advisory_skill` |
| `inputs`, `outputs` | The result fields the skill reads and the text it produces |
| `when_to_use` | The conditions under which the skill applies |
| `constraints` | Autonomy and approval limits (advisory only, no tool use) |
| `knowledge_dependencies` | Sections of `brain/onboarding/uc04_equipment_access.md` the skill quotes |
| `failure_behavior` | What the skill says when its input is missing |

A file without frontmatter, or with frontmatter that does not validate, is skipped silently by the loader.

## How skills are loaded and chosen

`SkillRegistry` (`src/sub_agent/skills.py`) reads every `*.md` file here except this README. `match_skills` picks the skills that apply to a case from its status and result, for example the requirements skill is added only when the case has configuration gaps or an out-of-matrix item. The role-package skill is added only when the case has `NO_ITEMS_REQUESTED`. The advisory lists the skills it applied on its last line.

## Rules for adding or changing a skill

1. It stays advisory: no state change, no approval, no ticket, no message sent.
2. It names only codes, states and roles that exist in the code. `tests/test_knowledge_accuracy.py` fails otherwise.
3. Its knowledge sections must exist in the brain file.
4. Add it to the table above.
