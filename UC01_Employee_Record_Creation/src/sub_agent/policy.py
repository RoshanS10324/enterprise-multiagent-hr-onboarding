"""Policy loading and authorization for UC-01.

Everything this module enforces comes from policy/uc01_policy.toml, which
is itself marked [Proposed] in full: roles, autonomy level and separation-
of-duties flags are drafts pending SME confirmation. This module only
loads and applies whatever that file says; it has no opinions of its own.
"""

import os
import tomllib
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

from .access import record_access_denied
from .errors import CaseNotFoundError, PolicyViolationCode, PolicyViolationError
from .models import ActorType, Approval, AuditEntry, Case, Checkpoint, Role, Viewer
from .ports import AuditLog

# Every checkpoint UC-01 raises an approval for, and every action name used
# in CaseService. Fail closed: the policy file MUST define all of these,
# or load_policy refuses to return a Policy at all.
REQUIRED_CHECKPOINTS: tuple[Checkpoint, ...] = (
    Checkpoint.HC_01_FORM,
    Checkpoint.HC_04_DATA,
    Checkpoint.HC_05_HRIS_WRITE,
    Checkpoint.HC_12_VARIANT,
)
REQUIRED_ACTIONS: tuple[str, ...] = (
    "compare_sources",
    "propose_form_change",
    "prepare_hris",
    "execute_hris_write",
)

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "policy" / "uc01_policy.toml"
# Overridable for deployments where the source-tree-relative default
# doesn't hold (e.g. a non-editable install layout). Empty/unset means
# "use DEFAULT_POLICY_PATH".
_ENV_POLICY_PATH = "UC01_POLICY_PATH"


class DecisionRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    roles: frozenset[Role]
    separation_of_duties: bool


class ActionRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    roles: frozenset[Role]


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Version of policy/uc01_policy.toml these rules were loaded from.
    policy_version: int
    autonomy_level: str
    decisions: dict[Checkpoint, DecisionRule]
    actions: dict[str, ActionRule]


def _parse_roles(raw_roles: object, context: str) -> frozenset[Role]:
    if not raw_roles:
        raise ValueError(f"{context}: roles must be a non-empty list")
    roles = set()
    for name in raw_roles:
        try:
            roles.add(Role(name))
        except ValueError:
            raise ValueError(f"{context}: unknown role {name!r}") from None
    return frozenset(roles)


def load_policy(path: Optional[Path] = None) -> Policy:
    if path is not None:
        policy_path = path
    elif os.environ.get(_ENV_POLICY_PATH):
        policy_path = Path(os.environ[_ENV_POLICY_PATH])
    else:
        policy_path = DEFAULT_POLICY_PATH
    with open(policy_path, "rb") as f:
        raw = tomllib.load(f)

    if "autonomy_level" not in raw:
        raise ValueError("policy file is missing autonomy_level")
    if "policy_version" not in raw:
        raise ValueError("policy file is missing policy_version")

    raw_decisions = raw.get("decisions", {})
    decisions: dict[Checkpoint, DecisionRule] = {}
    for checkpoint in REQUIRED_CHECKPOINTS:
        section = raw_decisions.get(checkpoint.value)
        if section is None:
            raise ValueError(f"policy file is missing decisions.{checkpoint.value}")
        if "separation_of_duties" not in section:
            raise ValueError(f"decisions.{checkpoint.value} is missing separation_of_duties")
        roles = _parse_roles(section.get("roles"), f"decisions.{checkpoint.value}")
        decisions[checkpoint] = DecisionRule(
            roles=roles,
            separation_of_duties=bool(section["separation_of_duties"]),
        )

    raw_actions = raw.get("actions", {})
    actions: dict[str, ActionRule] = {}
    for action_name in REQUIRED_ACTIONS:
        section = raw_actions.get(action_name)
        if section is None:
            raise ValueError(f"policy file is missing actions.{action_name}")
        roles = _parse_roles(section.get("roles"), f"actions.{action_name}")
        actions[action_name] = ActionRule(roles=roles)

    return Policy(
        policy_version=int(raw["policy_version"]),
        autonomy_level=raw["autonomy_level"],
        decisions=decisions,
        actions=actions,
    )


def record_policy_denied(
    audit_log: AuditLog,
    case_id: str,
    viewer: Viewer,
    scope: str,
    code: PolicyViolationCode,
    policy_version: int,
) -> None:
    """Record a POLICY_DENIED entry. One shape everywhere: `scope` is a
    checkpoint value (e.g. "HC_01_FORM") for a decision, or an action name
    (e.g. "propose_form_change") for an action-level rule like
    REQUESTER_MISMATCH. Unlike ACCESS_DENIED, this carries the case_id:
    by this point the case has already been confirmed visible to the
    viewer, so naming it doesn't leak anything new.

    `policy_version` records which revision of uc01_policy.toml produced
    the denial, so a past decision can be read against the rules that
    were in force then rather than the rules in force now.
    """
    audit_log.append(
        AuditEntry(
            case_id=case_id,
            actor_type=ActorType.HUMAN,
            actor_id=viewer.user_id,
            action="POLICY_DENIED",
            after={
                "code": code.value,
                "scope": scope,
                "role": viewer.role.value,
                "policy_version": policy_version,
            },
            reference=case_id,
        )
    )


def authorize_action(policy: Policy, viewer: Viewer, action: str, audit_log: AuditLog) -> None:
    rule = policy.actions[action]
    if viewer.role not in rule.roles:
        record_access_denied(audit_log, viewer, action)
        raise PolicyViolationError(PolicyViolationCode.ROLE_NOT_ALLOWED)


def authorize_decision(
    policy: Policy,
    viewer: Viewer,
    checkpoint: Checkpoint,
    case: Case,
    approval: Approval,
    audit_log: AuditLog,
) -> None:
    rule = policy.decisions[checkpoint]

    if viewer.role not in rule.roles:
        record_access_denied(audit_log, viewer, f"decide:{checkpoint.value}")
        raise PolicyViolationError(PolicyViolationCode.ROLE_NOT_ALLOWED)

    # Client-scoping only here (a hard tenant boundary: hidden, like U9).
    # The finer HR_OFFICER-must-be-assigned rule is a separate, visible
    # policy denial below (NOT_ASSIGNED_OFFICER), not folded into this
    # existence check - unlike access.case_visible, which conflates the
    # two for read-only U9 views.
    if case.client_id not in viewer.client_ids:
        record_access_denied(audit_log, viewer, f"decide:{checkpoint.value}")
        raise CaseNotFoundError()

    if viewer.role == Role.HR_OFFICER and case.hr_officer_id != viewer.user_id:
        record_policy_denied(
            audit_log,
            case.case_id,
            viewer,
            checkpoint.value,
            PolicyViolationCode.NOT_ASSIGNED_OFFICER,
            policy.policy_version,
        )
        raise PolicyViolationError(PolicyViolationCode.NOT_ASSIGNED_OFFICER)

    if (
        rule.separation_of_duties
        and approval.requested_by is not None
        and approval.requested_by == viewer.user_id
    ):
        record_policy_denied(
            audit_log,
            case.case_id,
            viewer,
            checkpoint.value,
            PolicyViolationCode.SELF_APPROVAL,
            policy.policy_version,
        )
        raise PolicyViolationError(PolicyViolationCode.SELF_APPROVAL)
