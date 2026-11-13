
"""UC-02 agent tools: the only operations an agent may perform.

Each tool is a thin, typed wrapper over :class:`OnboardingService`. The tool
layer adds **no business logic** — it cannot, because every rule it would need
(prerequisites, owner authorisation, evidence, approval gates, client
isolation) is already enforced inside the service and would simply be
duplicated here, with the usual consequence that the two copies drift.

Three properties matter:

**Allowlisted.** Every tool name appears in ``guardrails/policy.yaml`` under
``tools.read`` or ``tools.write_requires_approval``. The harness
``ToolGateway`` raises ``PolicyViolation`` for anything else, and an L0 agent
cannot call a write tool at all. The allowlist is the policy envelope's, not
this module's.

**Write tools are approval-gated.** The two write tools below refuse to run
without an approved gate decision. That check is the service's; the tool
simply has no code path that bypasses it.

**Results are facts, not claims.** Every tool returns what the service
actually returned. :func:`verify_task_state` exists so the agent can confirm a
write landed by re-reading it, rather than assuming the absence of an
exception means success.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from onboarding_task_management.contracts import (
    Actor,
    ChecklistContext,
    DecisionOutcome,
    GateName,
)
from onboarding_task_management.errors import OnboardingError
from onboarding_task_management.sub_agent.service import OnboardingService

#: Tool names. These strings are the contract with ``guardrails/policy.yaml``;
#: ``tests/test_agent.py`` asserts every one is on the allowlist.
GET_ONBOARDING = "uc02.get_onboarding"
GET_CHECKLIST = "uc02.get_checklist"
INSPECT_BLOCKERS = "uc02.inspect_blockers"
PREVIEW_CHECKLIST = "uc02.preview_checklist"
ASSESS_RISK = "uc02.assess_risk"
PREPARE_FOLLOWUP = "uc02.prepare_followup"
VERIFY_TASK_STATE = "uc02.verify_task_state"
REQUEST_APPROVAL = "uc02.request_approval"
UPDATE_TASK_STATE = "uc02.update_task_state"
GET_CONFIGURATION = "uc02.get_configuration"
CRITICAL_PATH = "uc02.critical_path"
ANALYSE_PLAN = "uc02.analyse_plan"
PROPOSE_REPLAN = "uc02.propose_replan"
PREPARE_TEAM_DRAFTS = "uc02.prepare_team_drafts"

#: Tools that only read. Safe for any autonomy level.
READ_TOOLS = (
    GET_ONBOARDING,
    GET_CHECKLIST,
    INSPECT_BLOCKERS,
    PREVIEW_CHECKLIST,
    ASSESS_RISK,
    PREPARE_FOLLOWUP,
    VERIFY_TASK_STATE,
    GET_CONFIGURATION,
    CRITICAL_PATH,
    ANALYSE_PLAN,
    PROPOSE_REPLAN,
    PREPARE_TEAM_DRAFTS,
)

#: Tools that change state. Every one needs an approved gate decision.
WRITE_TOOLS = (REQUEST_APPROVAL, UPDATE_TASK_STATE)

ALL_TOOLS = READ_TOOLS + WRITE_TOOLS


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one tool call.

    ``ok=False`` carries the error instead of raising, so the agent can record
    a failed step and report it honestly rather than aborting the run or —
    worse — treating a swallowed exception as success.
    """

    tool: str
    ok: bool
    data: Any = None
    error: str = ""
    error_code: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok


def _run(tool: str, fn: Callable[[], Any]) -> ToolResult:
    try:
        return ToolResult(tool=tool, ok=True, data=fn())
    except OnboardingError as exc:
        # Domain failure: expected, typed, reportable.
        return ToolResult(tool=tool, ok=False, error=exc.message, error_code=exc.code)


class UC02Tools:
    """The UC-02 tool surface, bound to one service and one acting actor.

    The actor is fixed at construction: an agent cannot widen its own scope
    mid-run by passing a different actor to a tool.
    """

    def __init__(self, service: OnboardingService, actor: Actor) -> None:
        self.service = service
        self.actor = actor
        self.calls: list[ToolResult] = []

    def _record(self, result: ToolResult) -> ToolResult:
        self.calls.append(result)
        return result

    # -- read --------------------------------------------------------------

    def get_configuration(self, onboarding_id: str) -> ToolResult:
        """The approved client configuration summary the plan was built from (ids, version, counts)."""

        def read():
            record = self.service.get(self.actor, onboarding_id)
            config = self.service.registry.get(record.client_id, on=record.joining_date)
            return {
                "client_id": config.client_id, "version": config.version,
                "approval_status": config.approval_status, "template_keys": sorted(t.key for t in config.templates),
                "assignment_rules": len(config.assignments), "pinned_to_record": record.config_version,
            }

        return self._record(_run(GET_CONFIGURATION, read))

    def critical_path(self, onboarding_id: str) -> ToolResult:
        """The chain of open tasks with the least room before day one."""
        return self._record(_run(CRITICAL_PATH, lambda: self.service.critical_path(self.actor, onboarding_id)))

    def analyse_plan(self, onboarding_id: str, as_of: date, analyst=None) -> ToolResult:
        """Structured, validated, advisory analysis. Changes nothing."""
        from onboarding_task_management.agent.analysis import analyse_with_fallback

        return self._record(_run(
            ANALYSE_PLAN, lambda: analyse_with_fallback(self.service, self.actor, onboarding_id, as_of, analyst)))

    def propose_replan(self, onboarding_id: str, as_of: date, analyst=None) -> ToolResult:
        """Re-planning PROPOSALS for HR (never applied): risks, delay impact and what to ask HR to decide."""
        from onboarding_task_management.agent.analysis import analyse_with_fallback

        def propose():
            a = analyse_with_fallback(self.service, self.actor, onboarding_id, as_of, analyst)
            return {"onboarding_id": onboarding_id, "recommendations": a.replan_recommendations,
                    "delay_impact": [d.model_dump(mode="json") for d in a.delay_impact],
                    "requires_hr_decision": True, "applied": False, "source": a.source}

        return self._record(_run(PROPOSE_REPLAN, propose))

    def prepare_team_drafts(self, onboarding_id: str) -> ToolResult:
        """Team-specific assignment drafts for a released plan. Drafts only; nothing is sent."""
        return self._record(_run(PREPARE_TEAM_DRAFTS, lambda: self.service.prepare_team_drafts(self.actor, onboarding_id)))

    def get_onboarding(self, onboarding_id: str) -> ToolResult:
        """The stored onboarding record (a snapshot; mutating it changes nothing)."""
        return self._record(
            _run(GET_ONBOARDING, lambda: self.service.get(self.actor, onboarding_id))
        )

    def get_checklist(self, onboarding_id: str, as_of: date) -> ToolResult:
        """The explained checklist: entries, exclusions, state buckets, issues."""
        return self._record(
            _run(
                GET_CHECKLIST,
                lambda: self.service.checklist(self.actor, onboarding_id, as_of),
            )
        )

    def inspect_blockers(self, onboarding_id: str, as_of: date) -> ToolResult:
        """Outstanding activities with owner, due date and the missing prerequisite."""
        return self._record(
            _run(
                INSPECT_BLOCKERS,
                lambda: self.service.outstanding_activities(
                    self.actor, onboarding_id, as_of
                ),
            )
        )

    def preview_checklist(self, context: ChecklistContext) -> ToolResult:
        """What a context *would* produce. Creates nothing."""
        return self._record(
            _run(
                PREVIEW_CHECKLIST,
                lambda: self.service.preview_checklist(self.actor, context),
            )
        )

    def assess_risk(self, onboarding_id: str, as_of: date) -> ToolResult:
        """Day-1 joining risk, computed by code — never estimated by a model."""
        return self._record(
            _run(
                ASSESS_RISK,
                lambda: self.service.joining_risk(self.actor, onboarding_id, as_of),
            )
        )

    def prepare_followup(self, onboarding_id: str, as_of: date) -> ToolResult:
        """Reminder **drafts** and escalation items.

        Preparing a draft is the terminal action. There is no transport in
        UC-02 and this tool adds none: delivery belongs to UC-06.
        """

        def prepare() -> dict[str, Any]:
            return {
                "drafts": self.service.reminder_drafts(
                    self.actor, onboarding_id, as_of
                ),
                "escalations": self.service.escalations(
                    self.actor, onboarding_id, as_of
                ),
            }

        return self._record(_run(PREPARE_FOLLOWUP, prepare))

    def verify_task_state(
        self, onboarding_id: str, task_id: str, expected_status: str
    ) -> ToolResult:
        """Re-read a task and confirm it really is in ``expected_status``.

        This is the verification step. A write tool returning without an
        exception is not proof; this re-reads the stored record and compares.
        """

        def verify() -> dict[str, Any]:
            record = self.service.get(self.actor, onboarding_id)
            task = record.task_by_id(task_id)
            if task is None:
                return {
                    "task_id": task_id,
                    "found": False,
                    "matches_expected": False,
                    "actual_status": None,
                }
            return {
                "task_id": task_id,
                "found": True,
                "actual_status": task.status.value,
                "expected_status": expected_status,
                "matches_expected": task.status.value == expected_status,
                "evidence_recorded": bool(task.evidence_ref),
            }

        return self._record(_run(VERIFY_TASK_STATE, verify))

    # -- write (approval-gated) -------------------------------------------

    def request_approval(
        self,
        onboarding_id: str,
        *,
        joining_date: date | None = None,
        location: str | None = None,
        project: str | None = None,
    ) -> ToolResult:
        """Raise a hard-gate approval request for a plan change.

        Raising the request is all the agent may do. The decision belongs to
        the gate's approver role, and the requester may not decide their own
        request (``approvals.ApprovalInbox``).
        """
        return self._record(
            _run(
                REQUEST_APPROVAL,
                lambda: self.service.request_change(
                    self.actor,
                    onboarding_id,
                    joining_date=joining_date,
                    location=location,
                    project=project,
                ),
            )
        )

    def update_task_state(
        self,
        onboarding_id: str,
        task_id: str,
        *,
        action: str,
        evidence_ref: str | None = None,
    ) -> ToolResult:
        """Start or complete a task through the service's own rules.

        Every check the service applies still applies: client isolation, owner
        authorisation, unmet prerequisites, onboarding state, and the evidence
        requirement for a task configured with ``evidence_required``. This
        wrapper cannot relax any of them.
        """
        if action not in ("start", "complete"):
            return self._record(
                ToolResult(
                    tool=UPDATE_TASK_STATE,
                    ok=False,
                    error=f"unsupported action {action!r}; expected 'start' or 'complete'",
                    error_code="POLICY_BREACH",
                )
            )

        def apply() -> Any:
            if action == "start":
                return self.service.start_task(self.actor, onboarding_id, task_id)
            return self.service.complete_task(
                self.actor, onboarding_id, task_id, evidence_ref
            )

        return self._record(_run(UPDATE_TASK_STATE, apply))

    # -- gate decision (not an agent tool) ---------------------------------

    def apply_approved_change(self, request_id: str) -> ToolResult:
        """Apply a change whose gate has already been approved by a human.

        Deliberately **not** in :data:`ALL_TOOLS`: the agent may request a
        change and may act once a human has approved it, but this call is
        driven by the approval arriving, not by the agent deciding to proceed.
        """
        return self._record(
            _run(
                UPDATE_TASK_STATE,
                lambda: self.service.apply_change(self.actor, request_id),
            )
        )


__all__ = [
    "ALL_TOOLS",
    "ASSESS_RISK",
    "GET_CHECKLIST",
    "GET_ONBOARDING",
    "INSPECT_BLOCKERS",
    "PREPARE_FOLLOWUP",
    "PREVIEW_CHECKLIST",
    "READ_TOOLS",
    "REQUEST_APPROVAL",
    "UPDATE_TASK_STATE",
    "VERIFY_TASK_STATE",
    "WRITE_TOOLS",
    "DecisionOutcome",
    "GateName",
    "ToolResult",
    "UC02Tools",
]
