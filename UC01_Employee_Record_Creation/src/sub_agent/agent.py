"""The UC-01 sub-agent's capability surface.

One place to see everything the sub-agent can do, and in what order. The
behaviour itself lives in `sub_agent`; this module only names
and groups it, so nothing here duplicates a rule or a decision.

Flow, in the order a case moves through it:

1. `handle_acceptance_email` -- trigger in, start gate and duplicate
   check run, case opened or held.
2. `handle_form_approved`    -- a held case resumes once the approved
   onboarding form exists.
3. `CaseService.compare_sources` / `resolve_discrepancy` -- differences
   against the approved form are flagged to HR, never overwritten.
4. `CaseService.propose_form_change` / `decide_form_change` -- changes to
   employee details after case creation.
5. `CaseService.prepare_hris` -> `decide_hris_write` ->
   `execute_hris_write` -- the record is prepared, a person approves, and
   only then is it written.

Every step is authorised against the caller's `Viewer` and written to the
audit log by the underlying modules.
"""

from .compare_sources import CompareResult, DiscrepancyDecision
from .deps import UC01Deps
from .hris_prepare import PrepareResult
from .hris_write import WriteOutcome
from .intake.email_intake import (
    IntakeDeps,
    IntakeOutcome,
    IntakeResult,
    handle_acceptance_email,
    handle_form_approved,
    list_pending_starts,
    parse_acceptance_email,
)
from .models import Role, Viewer
from .onboarding_entry import (
    OnboardingDeps,
    OpenCaseOutcome,
    OpenCaseRequest,
    open_onboarding_case,
)
from .policy import Policy, load_policy
from .service import CaseService, FormDecisionResult

__all__ = [
    "CaseService",
    "CompareResult",
    "DiscrepancyDecision",
    "FormDecisionResult",
    "IntakeDeps",
    "IntakeOutcome",
    "IntakeResult",
    "OnboardingDeps",
    "OpenCaseOutcome",
    "OpenCaseRequest",
    "Policy",
    "PrepareResult",
    "Role",
    "UC01Deps",
    "Viewer",
    "WriteOutcome",
    "handle_acceptance_email",
    "handle_form_approved",
    "list_pending_starts",
    "load_policy",
    "open_onboarding_case",
    "parse_acceptance_email",
]
