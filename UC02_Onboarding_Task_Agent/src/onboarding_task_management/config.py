"""Static settings and thresholds for UC-02.

Thresholds are mirrored in ``guardrails/policy.yaml``. CLAUDE.md rule 3 requires
gates/autonomy/allowlists to live in ``policy.yaml`` only. ``pyyaml`` is not
installed on this machine, so the prototype cannot parse that file yet; the
constants below are the single in-code copy and MUST be kept identical to the
YAML until :func:`load_policy` is implemented against ``PolicyStore``.
"""

from __future__ import annotations

from pathlib import Path

AGENT_ID = "onboarding-task-management"
VERSION = "0.1.0"

JURISDICTION = "AE"
DEFAULT_TIMEZONE = "Asia/Dubai"

#: Autonomy at launch and the ceiling the policy envelope permits.
AUTONOMY_LAUNCH = "L1"
AUTONOMY_CEILING = "L2"

#: A task still open this many business days before joining is a joining-date risk.
JOINING_RISK_DAYS_BEFORE = 2

#: Tasks due within this many days get a reminder draft.
REMINDER_LEAD_DAYS = 3

#: UC02-BR-10 escalation, from Business Framework §10:
#: "to the assigned HR officer ...; if there is no response within one working
#: day, to the HR operations lead." One working day = one business day.
ESCALATION_RESPONSE_DAYS = 1

#: Categories that must be complete for a candidate to be day-1 ready.
DAY_ONE_CRITICAL_CATEGORIES = frozenset(
    {"hr_record", "document_collection", "it_provisioning", "access_management"}
)

#: Repo root. The flat ``src/*.py`` placeholders left by the scaffold are not a
#: package; this prototype lives in ``src/onboarding_task_management/`` so that
#: ``parents[2]`` resolves to the repo root (review R-9).
AGENT_HOME = Path(__file__).resolve().parents[2]

POLICY_PATH = AGENT_HOME / "guardrails" / "policy.yaml"
