"""CaseService: the single entry point for UC-01. Every operation goes
through a Viewer and the policy layer first; nothing in this module calls
compare_sources/form_changes/hris_prepare/hris_write/variants/case_status
directly from outside it any more.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict

from .access import record_access_denied, require_visible_case
from .case_status import U9Deps
from .case_status import get_audit_trail as _get_audit_trail
from .case_status import verify_audit_integrity as _verify_audit_integrity
from .case_status import get_case_status as _get_case_status
from .case_status import list_pending_approvals as _list_pending_approvals
from .compare_sources import CompareDeps, CompareResult, DiscrepancyDecision
from .compare_sources import compare_sources as _compare_sources
from .compare_sources import resolve_discrepancy as _resolve_discrepancy
from .deps import UC01Deps
from .llm import LlmClient
from .errors import CaseNotFoundError, PolicyViolationCode, PolicyViolationError
from .form_changes import ProposeResult
from .form_changes import approve_form_change as _approve_form_change
from .form_changes import propose_form_change as _propose_form_change
from .form_changes import reject_form_change as _reject_form_change
from .hris_prepare import PrepareResult, prepare_hris_records
from .hris_write import WriteOutcome
from .hris_write import approve_hris_write as _approve_hris_write
from .hris_write import execute_hris_write as _execute_hris_write
from .hris_write import reject_hris_write as _reject_hris_write
from .models import (
    AuditEntry,
    Case,
    CaseStatusView,
    Checkpoint,
    Discrepancy,
    FormChangeRequest,
    HrisRecord,
    PendingApprovalView,
    SourceSnapshot,
    Viewer,
)
from .policy import Policy, authorize_action, authorize_decision, record_policy_denied
from .variants import decide_variant_approval as _decide_variant_approval


class FormDecisionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    approved: bool
    form_version: int
    case_id: str


# Which checkpoint each decision method expects its approval_id to belong
# to. Checked before authorize_decision so a wrong-type approval id (e.g.
# passing an HC_04_DATA approval to decide_form_change) is refused the
# same way as an unknown id, not with a confusing policy error.
_METHOD_CHECKPOINT = {
    "decide_form_change": Checkpoint.HC_01_FORM,
    "decide_hris_write": Checkpoint.HC_05_HRIS_WRITE,
    "decide_variant": Checkpoint.HC_12_VARIANT,
    "resolve_discrepancy": Checkpoint.HC_04_DATA,
}


class CaseService:
    def __init__(
        self,
        change_deps: UC01Deps,
        fingerprint_key: bytes,
        policy: Policy,
        llm: Optional[LlmClient] = None,
    ) -> None:
        self._deps = change_deps
        self._policy = policy
        self._compare_deps = CompareDeps(
            discrepancy_repo=change_deps.discrepancy_repo,
            approval_repo=change_deps.approval_repo,
            audit_log=change_deps.audit_log,
            fingerprint_key=fingerprint_key,
            llm=llm,
        )
        self._u9_deps = U9Deps(
            case_repo=change_deps.case_repo,
            form_repo=change_deps.form_repo,
            approval_repo=change_deps.approval_repo,
            discrepancy_repo=change_deps.discrepancy_repo,
            hris_record_repo=change_deps.hris_record_repo,
            audit_log=change_deps.audit_log,
        )

    # --- internal helpers -------------------------------------------------

    def _resolve_approval_case(
        self, viewer: Viewer, approval_id: str, operation: str, expected_checkpoint: Checkpoint | None = None
    ) -> tuple:
        """Resolve an approval id to (approval, its case).

        An unknown approval id, an approval belonging to another client's
        case, an HR_OFFICER's unassigned case, AND an approval of the
        wrong checkpoint type all raise the identical CaseNotFoundError -
        none of them should let a caller tell these apart.
        """
        approval = self._deps.approval_repo.get(approval_id)
        if approval is None:
            record_access_denied(self._deps.audit_log, viewer, operation)
            raise CaseNotFoundError()
        if expected_checkpoint is not None and approval.checkpoint != expected_checkpoint:
            record_access_denied(self._deps.audit_log, viewer, operation)
            raise CaseNotFoundError()
        case = require_visible_case(approval.case_id, viewer, self._deps.case_repo, self._deps.audit_log, operation)
        return approval, case

    def _resolve_discrepancy_case(self, viewer: Viewer, discrepancy_id: str, operation: str) -> tuple:
        discrepancy = self._deps.discrepancy_repo.get(discrepancy_id)
        if discrepancy is None:
            record_access_denied(self._deps.audit_log, viewer, operation)
            raise CaseNotFoundError()
        case = require_visible_case(
            discrepancy.case_id, viewer, self._deps.case_repo, self._deps.audit_log, operation
        )
        return discrepancy, case

    def _find_discrepancy_approval(self, case: Case, discrepancy_id: str):
        subject_ref = f"discrepancy:{discrepancy_id}"
        approval = next(
            (a for a in self._deps.approval_repo.list_for_case(case.case_id) if a.subject_ref == subject_ref),
            None,
        )
        if approval is None:
            # Shouldn't happen: compare_sources always raises exactly one
            # approval per discrepancy. Treat it like any other denial.
            raise CaseNotFoundError()
        return approval

    # --- U9: status, audit, worklist ---------------------------------------

    def get_status(self, viewer: Viewer, case_id: str) -> CaseStatusView:
        return _get_case_status(case_id, viewer, self._u9_deps)

    def get_audit_trail(self, viewer: Viewer, case_id: str) -> list[AuditEntry]:
        return _get_audit_trail(case_id, viewer, self._u9_deps)

    def verify_audit_integrity(self, viewer: Viewer):
        """(entries_checked, violations) over the complete audit log."""
        return _verify_audit_integrity(viewer, self._u9_deps)

    def list_pending_approvals(self, viewer: Viewer) -> list[PendingApprovalView]:
        return _list_pending_approvals(viewer, self._u9_deps)

    # --- U5: compare / resolve ----------------------------------------------

    def compare_sources(self, viewer: Viewer, case_id: str, snapshots: list[SourceSnapshot]) -> CompareResult:
        authorize_action(self._policy, viewer, "compare_sources", self._deps.audit_log)
        case = require_visible_case(case_id, viewer, self._deps.case_repo, self._deps.audit_log, "compare_sources")

        form = self._deps.form_repo.latest_approved(case.case_id)
        if form is None:
            raise ValueError(f"no approved form in force for case {case_id!r}")

        return _compare_sources(case, form, snapshots, self._compare_deps, actor_id=viewer.user_id)

    def resolve_discrepancy(self, viewer: Viewer, discrepancy_id: str, decision: DiscrepancyDecision) -> Discrepancy:
        discrepancy, case = self._resolve_discrepancy_case(viewer, discrepancy_id, "resolve_discrepancy")
        approval = self._find_discrepancy_approval(case, discrepancy_id)
        authorize_decision(self._policy, viewer, Checkpoint.HC_04_DATA, case, approval, self._deps.audit_log)

        return _resolve_discrepancy(discrepancy_id, decision, viewer.user_id, self._compare_deps)

    # --- U8: propose / decide form change -----------------------------------

    def propose_form_change(
        self, viewer: Viewer, request: FormChangeRequest, approver_id: str
    ) -> ProposeResult:
        authorize_action(self._policy, viewer, "propose_form_change", self._deps.audit_log)
        case = require_visible_case(
            request.case_id, viewer, self._deps.case_repo, self._deps.audit_log, "propose_form_change"
        )

        if request.requested_by != viewer.user_id:
            record_policy_denied(
                self._deps.audit_log,
                case.case_id,
                viewer,
                "propose_form_change",
                PolicyViolationCode.REQUESTER_MISMATCH,
                self._policy.policy_version,
            )
            raise PolicyViolationError(PolicyViolationCode.REQUESTER_MISMATCH)

        return _propose_form_change(request, self._deps, approver_id)

    def decide_form_change(
        self, viewer: Viewer, approval_id: str, approve: bool, note: str = ""
    ) -> FormDecisionResult:
        approval, case = self._resolve_approval_case(
            viewer, approval_id, "decide_form_change", _METHOD_CHECKPOINT["decide_form_change"]
        )
        authorize_decision(self._policy, viewer, Checkpoint.HC_01_FORM, case, approval, self._deps.audit_log)

        if approve:
            updated_case = _approve_form_change(approval_id, viewer.user_id, self._deps)
            return FormDecisionResult(
                approved=True, form_version=updated_case.current_form_version, case_id=updated_case.case_id
            )
        rejected_form = _reject_form_change(approval_id, viewer.user_id, note, self._deps)
        return FormDecisionResult(approved=False, form_version=rejected_form.version, case_id=case.case_id)

    # --- U6/U7: prepare / decide / execute HRIS write -----------------------

    def prepare_hris(self, viewer: Viewer, case_id: str) -> PrepareResult:
        authorize_action(self._policy, viewer, "prepare_hris", self._deps.audit_log)
        require_visible_case(case_id, viewer, self._deps.case_repo, self._deps.audit_log, "prepare_hris")

        return prepare_hris_records(case_id, self._deps, requested_by=viewer.user_id)

    def decide_hris_write(self, viewer: Viewer, approval_id: str, approve: bool, note: str = "") -> list[HrisRecord]:
        approval, case = self._resolve_approval_case(
            viewer, approval_id, "decide_hris_write", _METHOD_CHECKPOINT["decide_hris_write"]
        )
        authorize_decision(self._policy, viewer, Checkpoint.HC_05_HRIS_WRITE, case, approval, self._deps.audit_log)

        if approve:
            return _approve_hris_write(approval_id, viewer.user_id, self._deps)
        return _reject_hris_write(approval_id, viewer.user_id, note, self._deps)

    def execute_hris_write(self, viewer: Viewer, case_id: str) -> WriteOutcome:
        authorize_action(self._policy, viewer, "execute_hris_write", self._deps.audit_log)
        require_visible_case(case_id, viewer, self._deps.case_repo, self._deps.audit_log, "execute_hris_write")

        return _execute_hris_write(case_id, self._deps)

    # --- HC_12: variant decisions --------------------------------------------

    def decide_variant(self, viewer: Viewer, approval_id: str, approve: bool, note: str = ""):
        approval, case = self._resolve_approval_case(
            viewer, approval_id, "decide_variant", _METHOD_CHECKPOINT["decide_variant"]
        )
        authorize_decision(self._policy, viewer, Checkpoint.HC_12_VARIANT, case, approval, self._deps.audit_log)

        return _decide_variant_approval(approval_id, viewer.user_id, approve, note, self._deps)
