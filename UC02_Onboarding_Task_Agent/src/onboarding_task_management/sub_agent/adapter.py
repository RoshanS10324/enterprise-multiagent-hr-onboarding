"""UC-02 sub-agent: a thin adapter over the existing onboarding service.

This module contains **no onboarding business logic**. Every rule — start
conditions, dynamic checklist selection, dependencies, due dates, owner
authorisation, approval gates, evidence, client isolation, audit — stays in
``src/onboarding_task_management/`` and is invoked, not reimplemented. What
lives here is the wiring between an inbound request, the deterministic fake
LLM, the existing service, and the mock Outlook connector.

One workflow run, in order:

1. validate the request (pydantic, then the service's own preconditions);
2. resolve the onboarding context — an inline trigger or an approved
   synthetic fixture under ``data/``;
3. create the onboarding, or recognise one that already exists;
4. generate the explained checklist from the rules engine;
5. ask the fake LLM to interpret it and recommend one action;
6. let :class:`UC02Agent` validate that advice, pick an allowlisted tool,
   execute it and **verify the stored result**;
7. optionally prepare reminder drafts and hand them to the mock connector;
8. return a structured result, including anything that was refused.

Idempotency: onboarding ids are derived, so a repeated request for the same
employee is recognised and returns the existing case with ``created=False``
instead of raising or creating a second one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from onboarding_task_management.agent.loop import UC02Agent
from onboarding_task_management.contracts import Actor, Role, StatusUpdate
from onboarding_task_management.errors import (
    DuplicateOnboardingError,
    OnboardingError,
)
from onboarding_task_management.sub_agent.connectors.outlook.base import (
    DeliveryStatus,
    OutlookMessage,
)
from onboarding_task_management.sub_agent.connectors.outlook.mock import (
    MockOutlookConnector,
    message_from_draft,
)
from onboarding_task_management.sub_agent.fakes import (
    make_fake_llm,
    make_mock_outlook,
)
from onboarding_task_management.sub_agent.service import (
    OnboardingService,
    OnboardingTrigger,
)
from onboarding_task_management.synthetic import build_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
FIXTURE_DIR = DATA_DIR / "mock"
CANONICAL_FIXTURE = DATA_DIR / "UC02_Synthetic_Onboarding_Test_Data.json"

#: Fixed evaluation date. UC-02 never reads the clock in business logic, so a
#: caller that omits ``as_of`` gets this rather than a non-reproducible today.
DEFAULT_AS_OF = date(2026, 10, 26)


class WorkflowError(OnboardingError):
    """The request could not be turned into a workflow run."""

    code = "MISSING_DATA"


def available_fixtures() -> list[str]:
    """Synthetic fixtures the API will accept by name."""
    names = []
    if CANONICAL_FIXTURE.is_file():
        names.append(CANONICAL_FIXTURE.name)
    if FIXTURE_DIR.is_dir():
        names += sorted(p.name for p in FIXTURE_DIR.glob("*.json"))
    return names


def load_fixture(name: str) -> dict[str, Any]:
    """Read one approved synthetic fixture.

    Only a bare file name is accepted, and it must already be on
    :func:`available_fixtures`. That closes the obvious path-traversal hole
    without needing to reason about ``..`` segments.
    """
    if name not in available_fixtures():
        raise WorkflowError(
            "unknown fixture", fixture=name, available=len(available_fixtures())
        )
    path = CANONICAL_FIXTURE if name == CANONICAL_FIXTURE.name else FIXTURE_DIR / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError("fixture could not be read", fixture=name) from exc


#: ``llm_behaviour`` value that routes advice through the injected LLM client.
GATEWAY_BEHAVIOUR = "gateway"


@dataclass
class WorkflowRequest:
    """What the sub-agent needs to run. Validated before anything happens."""

    trigger: dict[str, Any] | None = None
    fixture: str | None = None
    as_of: date = DEFAULT_AS_OF
    actor_id: str = "U-HR-1"
    actor_role: Role | str = Role.HR_OPS
    #: ``"offline"``, ``"gateway"`` (injected central ``LLMClient``) or one of
    #: the scripted fake behaviours.
    llm_behaviour: str = "scripted"
    #: Prepare reminder drafts and offer them to the mock Outlook connector.
    prepare_reminders: bool = False
    #: Approval id authorising simulated delivery. Without it the connector
    #: refuses — that refusal is a demonstrated outcome, not a failure.
    outlook_approval_id: str | None = None

    def resolve_trigger(self) -> dict[str, Any]:
        if self.trigger is not None and self.fixture is not None:
            raise WorkflowError("supply either 'trigger' or 'fixture', not both")
        if self.fixture is not None:
            return load_fixture(self.fixture)
        if self.trigger is not None:
            return self.trigger
        raise WorkflowError("one of 'trigger' or 'fixture' is required")


@dataclass
class WorkflowResult:
    """Everything the run did, and everything it refused to do."""

    onboarding_id: str
    client_id: str
    employee_id: str
    created: bool
    as_of: date
    checklist: dict[str, Any] = field(default_factory=dict)
    advice: dict[str, Any] = field(default_factory=dict)
    agent_run: dict[str, Any] = field(default_factory=dict)
    reminders: dict[str, Any] = field(default_factory=dict)
    audit_actions: list[str] = field(default_factory=list)
    #: Shared identifiers echoed from the trigger (``None`` if not supplied).
    case_id: str | None = None
    correlation_id: str | None = None
    #: The plan version the checklist was generated for (``OnboardingRecord.plan_version``).
    plan_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "onboarding_id": self.onboarding_id,
            "client_id": self.client_id,
            "employee_id": self.employee_id,
            "created": self.created,
            "as_of": self.as_of.isoformat(),
            "checklist": self.checklist,
            "advice": self.advice,
            "agent_run": self.agent_run,
            "reminders": self.reminders,
            "audit_actions": self.audit_actions,
            "case_id": self.case_id,
            "correlation_id": self.correlation_id,
        }


class UC02SubAgent:
    """Adapter binding the fake LLM, the UC-02 service and mock Outlook.

    The service is held for the lifetime of the instance so repeated
    requests see the same records — which is what makes duplicate detection
    and idempotency observable through the API.
    """

    def __init__(
        self,
        service: OnboardingService | None = None,
        outlook: MockOutlookConnector | None = None,
        llm_client: Any | None = None,
    ) -> None:
        self.service = service or OnboardingService(build_registry())
        self.outlook = outlook or make_mock_outlook()
        #: Injected ``hr_onboarding_common.llm.LLMClient`` — normally the
        #: orchestrator's ``AgentContext.llm``. Used when a request asks for
        #: ``llm_behaviour="gateway"``; never constructed here.
        self.llm_client = llm_client

    def _advisor(self, behaviour: str):
        """The advisor for one request.

        ``"offline"``  the deterministic rules engine;
        ``"gateway"``  the injected ``LLMClient``, or — when none was injected —
                       whatever :func:`build_advisor` selects from ``LLM_MODE``
                       (offline by default);
        anything else  the scripted fake LLM behaviour of that name.
        """
        from onboarding_task_management.agent.advisor import OfflineAdvisor, build_advisor

        if behaviour == "offline":
            return OfflineAdvisor()
        if behaviour == GATEWAY_BEHAVIOUR:
            return build_advisor(self.llm_client, fake=False)
        return make_fake_llm(behaviour)

    # -- helpers ------------------------------------------------------------

    def _actor(self, request: WorkflowRequest, client_id: str) -> Actor:
        role = Role(request.actor_role) if isinstance(request.actor_role, str) else request.actor_role
        return Actor(
            actor_id=request.actor_id,
            role=role,
            client_id=client_id,
        )

    def _existing_for(self, actor: Actor, employee_id: str):
        for record in self.service._client_records(actor.client_id).values():
            if record.employee_id == employee_id and record.status.value != "cancelled":
                return record
        return None

    # -- the workflow -------------------------------------------------------

    def run(self, request: WorkflowRequest) -> WorkflowResult:
        payload = request.resolve_trigger()
        trigger = OnboardingTrigger.model_validate(payload)
        actor = self._actor(request, trigger.client_id)

        created = True
        try:
            record = self.service.create_onboarding(actor, trigger)
        except DuplicateOnboardingError:
            # Idempotent: the same employee is not onboarded twice. Return the
            # case that already exists rather than failing the caller.
            existing = self._existing_for(actor, trigger.employee_id)
            if existing is None:
                raise
            record = existing
            created = False

        view = self.service.checklist(actor, record.onboarding_id, request.as_of)

        advisor = self._advisor(request.llm_behaviour)
        agent = UC02Agent(
            self.service, actor, advisor=advisor, store=self.service.store
        )
        run = agent.run(record.onboarding_id, request.as_of)

        result = WorkflowResult(
            onboarding_id=record.onboarding_id,
            client_id=record.client_id,
            employee_id=record.employee_id,
            created=created,
            as_of=request.as_of,
            checklist=self._summarise(view),
            advice=self._advice(run),
            agent_run=self._run_summary(run),
            case_id=record.case_id,
            correlation_id=record.correlation_id,
            plan_version=record.plan_version,
        )
        if request.prepare_reminders:
            result.reminders = self._reminders(actor, record.onboarding_id, request)
        result.audit_actions = sorted(set(self.service.audit.actions_for(actor)))
        return result

    def apply_status_update(
        self,
        payload: dict[str, Any],
        *,
        client_id: str,
        actor_id: str,
        actor_role: Role | str = Role.SYSTEM,
    ):
        """Apply one inbound status report from another workflow.

        ``payload`` is validated as a :class:`StatusUpdate`. ``client_id`` and
        ``actor_id`` are the *authenticated* tenant and caller, supplied by
        the integration layer and never read from the payload; a payload for
        another client raises ``DataIsolationError``. When the service was built with
        ``source_principals`` the report is accepted only if that actor is a
        registered reporter for ``payload['source']``.

        Business rejections come back in the result (see
        :mod:`status_updates`); malformed payloads raise ``ValueError``.
        """
        update = StatusUpdate.model_validate(payload)
        role = Role(actor_role) if isinstance(actor_role, str) else actor_role
        actor = Actor(actor_id=actor_id, role=role, client_id=client_id)
        return self.service.apply_status_update(
            actor, update.onboarding_id, update
        )

    # -- shaping ------------------------------------------------------------

    @staticmethod
    def _summarise(view) -> dict[str, Any]:
        return {
            "revision": view.revision,
            "total": len(view.entries),
            "excluded": len(view.excluded),
            "ready": list(view.ready_task_ids),
            "blocked": list(view.blocked_task_ids),
            "in_progress": list(view.in_progress_task_ids),
            "complete": list(view.complete_task_ids),
            "overdue": list(view.overdue_task_ids),
            "awaiting_approval": list(view.awaiting_approval_task_ids),
            "review_required": view.review_required,
            "issues": [
                {"code": i.code, "severity": i.severity.value, "subject": i.subject}
                for i in view.issues
            ],
            "tasks": [
                {
                    "task_id": e.task_id,
                    "template_key": e.template_key,
                    "status": e.status.value,
                    "owner": e.owner.value,
                    "due_date": e.due_date.isoformat(),
                    "blocked_by": list(e.blocked_by_keys),
                    "reason_included": e.decision.reason,
                }
                for e in view.entries
            ],
        }

    @staticmethod
    def _advice(run) -> dict[str, Any]:
        if run.advice is None:
            return {}
        return {
            "summary": run.advice.summary,
            "recommended_action": run.advice.recommended_action,
            "target_task_id": run.advice.target_task_id,
            "rationale": run.advice.rationale,
            "confidence": run.advice.confidence,
            "source": run.advice.source,
            "degraded": run.advice.degraded,
            "degraded_reason": run.advice.degraded_reason,
            "blockers": [
                {"task_id": b.task_id, "template_key": b.template_key, "reason": b.reason}
                for b in run.advice.blockers
            ],
        }

    @staticmethod
    def _run_summary(run) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "advisor": run.advisor,
            "model": run.model,
            "degraded": run.degraded,
            "degraded_reason": run.degraded_reason,
            "verified": run.verified,
            "actions_taken": list(run.actions_taken),
            "tool_calls": list(run.tool_calls),
            "exceptions": list(run.exceptions),
            "proposals": [
                {
                    "action": p.action,
                    "subject_id": p.subject_id,
                    "requires_approval_by": p.requires_approval_by,
                }
                for p in run.proposals
            ],
            "steps": [
                {"name": s.name, "ok": s.ok, "tool": s.tool, "detail": s.detail}
                for s in run.steps
            ],
        }

    def _reminders(
        self, actor: Actor, onboarding_id: str, request: WorkflowRequest
    ) -> dict[str, Any]:
        """Prepare drafts and offer them to the MOCK connector. Sends nothing."""
        drafts = self.service.reminder_drafts(actor, onboarding_id, request.as_of)
        receipts = []
        for draft in drafts:
            message: OutlookMessage = message_from_draft(
                draft, approval_id=request.outlook_approval_id
            )
            receipt = self.outlook.send(message)
            receipts.append(
                {
                    "idempotency_key": receipt.idempotency_key,
                    "status": receipt.status.value,
                    "message_id": receipt.message_id,
                    "detail": receipt.detail,
                }
            )
        return {
            "transport": "mock-outlook",
            "real_email_sent": False,
            "drafts_prepared": len(drafts),
            # UC-02 itself never marks a draft as sent.
            "draft_delivery_status": sorted({d.delivery_status for d in drafts}),
            "simulated_accepted": sum(
                1 for r in receipts if r["status"] == DeliveryStatus.SIMULATED_SENT.value
            ),
            "receipts": receipts,
            "outbox_size": self.outlook.sent_count(),
        }


__all__ = [
    "CANONICAL_FIXTURE",
    "DEFAULT_AS_OF",
    "FIXTURE_DIR",
    "GATEWAY_BEHAVIOUR",
    "UC02SubAgent",
    "WorkflowError",
    "WorkflowRequest",
    "WorkflowResult",
    "available_fixtures",
    "load_fixture",
]
