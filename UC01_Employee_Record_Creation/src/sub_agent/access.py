"""Case visibility and role-capability rules for U9. [Proposed]: all rules
here (client scoping, role capability sets, salary/PII limits) are drafts
to confirm with the process SME; see docs/known-gaps.md.
"""

from .errors import AccessDeniedError, CaseNotFoundError
from .models import ActorType, AuditEntry, Case, Role, Viewer
from .models.common import new_id
from .ports import AuditLog, CaseRepository

# Role capability sets (framework GR-16 client scoping; all [Proposed]).
STATUS_VIEW_ROLES = frozenset({Role.HR_OFFICER, Role.HR_MANAGER, Role.CLIENT_USER, Role.AUDITOR})
AUDIT_TRAIL_ROLES = frozenset({Role.HR_OFFICER, Role.HR_MANAGER, Role.AUDITOR})
WORKLIST_ROLES = frozenset({Role.HR_OFFICER, Role.HR_MANAGER})
CANDIDATE_NAME_ROLES = frozenset({Role.HR_OFFICER, Role.HR_MANAGER, Role.CLIENT_USER})
# Audit-chain verification spans every client in the process, so it is
# narrower than AUDIT_TRAIL_ROLES: an HR role scoped to one client must
# not learn how much audit activity the others generate.
AUDIT_INTEGRITY_ROLES = frozenset({Role.AUDITOR})


def case_visible(viewer: Viewer, case: Case) -> bool:
    if case.client_id not in viewer.client_ids:
        return False
    if viewer.role == Role.HR_OFFICER:
        return case.hr_officer_id == viewer.user_id
    # HR_MANAGER, CLIENT_USER, AUDITOR see every case of their clients.
    return True


def record_access_denied(audit_log: AuditLog, viewer: Viewer, operation: str) -> None:
    # No case_id, no ids, no details: an ACCESS_DENIED entry must never
    # reveal whether the thing being asked about even exists. Shared with
    # policy.py's authorize_action, which uses the identical format.
    audit_log.append(
        AuditEntry(
            request_id=new_id(),
            actor_type=ActorType.HUMAN,
            actor_id=viewer.user_id,
            action="ACCESS_DENIED",
            after={"role": viewer.role.value, "operation": operation},
            reference=viewer.user_id,
            # The VIEWER's own client (deterministic when they serve several),
            # never the target case's - so the entry still cannot reveal
            # whether the case exists.
            client_id=sorted(viewer.client_ids)[0],
        )
    )


def require_role(viewer: Viewer, allowed_roles: frozenset[Role], audit_log: AuditLog, operation: str) -> None:
    if viewer.role not in allowed_roles:
        record_access_denied(audit_log, viewer, operation)
        raise AccessDeniedError()


def require_visible_case(
    case_id: str,
    viewer: Viewer,
    case_repo: CaseRepository,
    audit_log: AuditLog,
    operation: str,
) -> Case:
    # A missing case and a case the viewer may not see must be
    # indistinguishable to the caller, otherwise the error itself leaks
    # whether the case exists.
    case = case_repo.get(case_id)
    if case is None or not case_visible(viewer, case):
        record_access_denied(audit_log, viewer, operation)
        raise CaseNotFoundError()
    return case
