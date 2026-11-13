"""The LLM layer: interpretation and recommendation only.

Division of responsibility (ADR-003, "models rate evidence, code computes
bands", applied to UC-02):

================================  =========================================
The model MAY                     The model MAY NOT
================================  =========================================
summarise the situation           choose which tasks exist
explain why work is blocked       change a due date or a dependency
rank which blocker to tackle      grant a permission or skip an approval
recommend one allowlisted tool    execute anything
================================  =========================================

Everything in the right-hand column is decided by :mod:`selection`,
:mod:`planner`, :mod:`tracker` and :mod:`approvals`. The advice returned here
is **validated against those facts** by :func:`validate_advice` before the
agent acts on it, so a wrong — or adversarial — model answer degrades to "no
recommendation", never to an unauthorised action.

Three implementations, and the difference between them matters:

:class:`OfflineAdvisor`
    A **rule-based advisor, not a model**. It calls :func:`derive_action` on
    the rules engine directly — no prompt, no payload, no schema round-trip.
    The safe fallback when nothing else is available.

:class:`LiteLLMAdvisor`
    The model-backed path: assembles the prompt, sends the ``<input>`` block,
    receives a schema-validated :class:`AgentAdvice`. Its client is an
    ``hr_onboarding_common.llm.LLMClient`` — normally the orchestrator's
    ``AgentContext.llm``, or ``LiteLLMGateway`` built from the common
    settings when ``LLM_MODE`` is online. That gateway is the only permitted
    route to a model (ADR-005); this module imports no provider SDK and reads
    no provider key.

:class:`~onboarding_task_management.agent.fake_llm.FakeLLMAdvisor`
    The same :class:`LiteLLMAdvisor` code driven by a deterministic scripted
    client. Every line of adapter logic a real gateway would exercise runs
    here too; only the network call is replaced. Labelled as a fake in its
    ``source`` and model id so no run can be mistaken for a real call.

:func:`build_advisor` picks between them from the environment and never fails
over to a fabricated answer: if a configured model errors, the failure is
reported and the deterministic advisor takes over, with ``degraded=True`` on
the result so the caller can see that a model call did not succeed.
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from onboarding_task_management.contracts import ChecklistView, JoiningRisk

#: Actions the advisor is allowed to name. A recommendation outside this set
#: is rejected by :func:`validate_advice`.
RECOMMENDABLE_ACTIONS = (
    "inspect_blockers",
    "progress_ready_task",
    "request_date_change",
    "prepare_followup",
    "escalate_configuration_review",
    "no_action_required",
)

SYSTEM_PREAMBLE = """\
You are the analysis layer of UC-02, Onboarding Task Management.

You receive a checklist that a deterministic rules engine has already
computed. Your job is to explain it and to recommend ONE next action.

Rules you must follow:
- Never invent a task, an owner, a due date, a legal requirement or a client
  policy. Use only the facts given to you.
- Never recommend granting access, skipping an approval, or marking work
  complete without the evidence the configuration requires.
- Recommend exactly one action, chosen from the allowed list.
- If the facts do not support any action, recommend no_action_required.
Your recommendation is advisory. Code decides whether it is permitted.
"""


class Blocker(BaseModel):
    """One thing standing in the way, stated in terms of the record."""

    task_id: str
    template_key: str = ""
    reason: str


class AgentAdvice(BaseModel):
    """Structured advice. Passed to the model as the response schema."""

    summary: str = Field(description="Two sentences at most, plain language.")
    blockers: list[Blocker] = Field(default_factory=list)
    recommended_action: str = Field(
        description="Exactly one of the allowed action names."
    )
    target_task_id: str | None = Field(
        default=None, description="Task the action applies to, when relevant."
    )
    rationale: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Set by code, never by the model.
    source: str = "offline"
    degraded: bool = False
    degraded_reason: str = ""


@runtime_checkable
class Advisor(Protocol):
    """Produces :class:`AgentAdvice` from an already-computed checklist."""

    name: str

    def advise(self, view: ChecklistView, risk: JoiningRisk) -> AgentAdvice: ...


# ---------------------------------------------------------------------------
# Facts handed to the advisor
# ---------------------------------------------------------------------------


def advice_payload(view: ChecklistView, risk: JoiningRisk) -> dict:
    """The facts a model is allowed to see.

    Identifiers, statuses, dates and counts only — no name, email, nationality
    or contact detail exists in any UC-02 contract, so none can leak here.
    Titles are client-configured task names, not personal data.
    """
    return {
        "onboarding_id": view.onboarding_id,
        "as_of": view.as_of.isoformat(),
        "joining_date": view.joining_date.isoformat(),
        "days_to_joining": risk.days_to_joining,
        "revision": view.revision,
        "onboarding_status": view.onboarding_status.value,
        "counts": {
            "total": len(view.entries),
            "ready": len(view.ready_task_ids),
            "blocked": len(view.blocked_task_ids),
            "in_progress": len(view.in_progress_task_ids),
            "complete": len(view.complete_task_ids),
            "overdue": len(view.overdue_task_ids),
            "awaiting_approval": len(view.awaiting_approval_task_ids),
        },
        "at_risk": risk.at_risk,
        "risk_reasons": list(risk.reasons),
        "pending_gate_request_ids": list(view.pending_gate_request_ids),
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
                "is_overdue": e.is_overdue,
                "blocked_by": list(e.blocked_by_keys),
                "evidence_required": e.evidence_required,
                "approval_role": e.approval_role.value if e.approval_role else None,
            }
            for e in view.entries
        ],
        "allowed_actions": list(RECOMMENDABLE_ACTIONS),
    }


# ---------------------------------------------------------------------------
# Deterministic advisor
# ---------------------------------------------------------------------------


def derive_blockers(view: ChecklistView) -> list[Blocker]:
    """Blockers stated from the record: overdue first, then blocked tasks."""
    blockers: list[Blocker] = []
    for entry in view.entries:
        if entry.is_overdue:
            blockers.append(
                Blocker(
                    task_id=entry.task_id,
                    template_key=entry.template_key,
                    reason=(
                        f"overdue by {entry.days_overdue} business day(s); "
                        f"owner {entry.owner.value}"
                    ),
                )
            )
        elif entry.blocked_by_keys:
            blockers.append(
                Blocker(
                    task_id=entry.task_id,
                    template_key=entry.template_key,
                    reason="waiting on " + ", ".join(entry.blocked_by_keys),
                )
            )
    return blockers


def derive_action(view: ChecklistView, risk: JoiningRisk) -> tuple[str, str | None, str]:
    """``(action, target_task_id, rationale)`` from the rules engine alone.

    Precedence is deliberate and fixed: a configuration problem outranks
    everything, because planning on a configuration nobody has confirmed is
    how wrong work gets done. Then day-1 risk, then overdue work, then
    ordinary progress.
    """
    if view.review_required:
        subjects = sorted({i.subject for i in view.issues if i.severity.value != "informational"})
        return (
            "escalate_configuration_review",
            None,
            "the checklist carries unresolved configuration issues ("
            + ", ".join(subjects)
            + "); UC-02 does not guess a client or legal requirement",
        )
    if risk.at_risk:
        return (
            "prepare_followup",
            None,
            "day-1 readiness is at risk: " + "; ".join(risk.reasons),
        )
    if view.overdue_task_ids:
        return (
            "prepare_followup",
            view.overdue_task_ids[0],
            f"{len(view.overdue_task_ids)} task(s) are past their due date",
        )
    if view.ready_task_ids:
        return (
            "progress_ready_task",
            view.ready_task_ids[0],
            f"{len(view.ready_task_ids)} task(s) have every prerequisite met",
        )
    if view.blocked_task_ids:
        return (
            "inspect_blockers",
            view.blocked_task_ids[0],
            "nothing is startable; every open task is waiting on a prerequisite",
        )
    return ("no_action_required", None, "no open work remains on this plan")


class OfflineAdvisor:
    """Deterministic advisor. No model, no network, no randomness."""

    name = "offline"

    def advise(self, view: ChecklistView, risk: JoiningRisk) -> AgentAdvice:
        action, target, rationale = derive_action(view, risk)
        blockers = derive_blockers(view)
        summary = (
            f"{len(view.entries)} task(s) on revision {view.revision}: "
            f"{len(view.complete_task_ids)} complete, {len(view.ready_task_ids)} ready, "
            f"{len(view.blocked_task_ids)} blocked, {len(view.overdue_task_ids)} overdue. "
            f"{'Day-1 readiness is at risk.' if risk.at_risk else 'Day-1 readiness is on track.'}"
        )
        return AgentAdvice(
            summary=summary,
            blockers=blockers,
            recommended_action=action,
            target_task_id=target,
            rationale=rationale,
            # Derived facts, not an estimate: the rules engine is certain.
            confidence=1.0,
            source=self.name,
        )


# ---------------------------------------------------------------------------
# LiteLLM advisor
# ---------------------------------------------------------------------------


try:
    from hr_onboarding_common.llm import LLMError, LLMOutputError, LLMRefusal, ModelTier  # type: ignore[import-untyped]
    from hr_onboarding_common.llm import FakeLLM as _CommonFakeLLM  # type: ignore[import-untyped]
    from hr_onboarding_common.llm import input_block as _common_input_block  # type: ignore[import-untyped]

    COMMON_LLM_AVAILABLE = True
except ImportError:  # UC-02 standalone: pydantic only, no central gateway
    COMMON_LLM_AVAILABLE = False

    class _Tier(StrEnum):
        """Local mirror of ``hr_onboarding_common.llm.ModelTier``.

        Used only when the common package is absent, so the adapter below —
        and the fake LLM that exercises it — stays runnable on pydantic alone.
        """

        FRONTIER = "frontier"
        WORKHORSE = "workhorse"
        FAST = "fast"

    class LLMError(RuntimeError):  # type: ignore[no-redef]
        """Stand-in so ``except LLMError`` stays valid without the gateway."""

    class LLMRefusal(LLMError):  # type: ignore[no-redef]
        pass

    class LLMOutputError(LLMError):  # type: ignore[no-redef]
        pass

    ModelTier = _Tier  # type: ignore[misc,assignment]
    _CommonFakeLLM = None  # type: ignore[assignment,misc]
    _common_input_block = None  # type: ignore[assignment]

#: The gateway task name for UC-02 advice. Used by the gateway call and by
#: :func:`register_fake_handler`.
ADVISE_TASK = "uc02.advise"


def default_tier() -> object:
    """``ModelTier.WORKHORSE`` from the central gateway (local mirror if absent)."""
    return ModelTier.WORKHORSE


def input_block(payload: dict) -> str:
    """Render the structured facts into the user turn.

    Delegates to ``hr_onboarding_common.llm.input_block`` so the wire format
    is the platform's, and falls back to the same byte layout when the common
    package is not installed. Sorted keys keep the prompt cache-stable.
    """
    if _common_input_block is not None:
        return _common_input_block(payload)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1)
    return "<input>\n" + body + "\n</input>"


class LiteLLMAdvisor:
    """Model-backed advisor. One code path, two possible clients.

    Holds no provider SDK and no key: the injected ``LLMClient`` (in
    production ``hr_onboarding_common.llm.LiteLLMGateway``) owns routing,
    retries, fallbacks and callbacks (ADR-005 §2). If the gateway raises, this class
    does not invent an answer — :func:`advise_with_fallback` degrades to the
    deterministic advisor and marks the result degraded.

    The same class drives
    :class:`~onboarding_task_management.agent.fake_llm.FakeLLMClient`, so the
    prompt assembly, schema round-trip and source tagging exercised in tests
    are the ones a real gateway would use.
    """

    name = "litellm"

    def __init__(self, client: object, tier: object | None = None) -> None:
        self.client = client
        self.tier = tier if tier is not None else default_tier()

    def model(self) -> str:
        return self.client.model_for(self.tier)  # type: ignore[attr-defined]

    def advise(self, view: ChecklistView, risk: JoiningRisk) -> AgentAdvice:
        payload = advice_payload(view, risk)
        user = (
            "Explain this onboarding checklist and recommend one next action.\n\n"
            + input_block(payload)
        )
        advice = self.client.structured(  # type: ignore[attr-defined]
            task=ADVISE_TASK,
            tier=self.tier,
            system=SYSTEM_PREAMBLE,
            user=user,
            schema=AgentAdvice,
            max_tokens=2000,
        )
        advice.source = f"{self.source_label()}:{self.model()}"
        return advice

    def source_label(self) -> str:
        """``fake_llm`` when the client is the common offline ``FakeLLM``.

        The orchestrator's offline ``AgentContext.llm`` is a ``FakeLLM``; its
        advice must not read ``litellm:...`` and be mistaken for a real call.
        """
        if _CommonFakeLLM is not None and isinstance(self.client, _CommonFakeLLM):
            return "fake_llm"
        return self.name


# ---------------------------------------------------------------------------
# Validation — code has the last word
# ---------------------------------------------------------------------------


def validate_advice(
    advice: AgentAdvice, view: ChecklistView, risk: JoiningRisk
) -> AgentAdvice:
    """Reject advice the record does not support.

    A model that names an unknown action, points at a task that is not in this
    plan, or claims a ready task when none is ready, is overruled: the advice
    is replaced by the deterministic recommendation and marked degraded. This
    is what stops a compromised or confused model from steering the workflow.
    """
    problems: list[str] = []

    if advice.recommended_action not in RECOMMENDABLE_ACTIONS:
        problems.append(
            f"action {advice.recommended_action!r} is not an allowed action"
        )

    known_task_ids = {entry.task_id for entry in view.entries}
    if advice.target_task_id is not None and advice.target_task_id not in known_task_ids:
        problems.append(
            f"target task {advice.target_task_id!r} is not in this onboarding"
        )

    if advice.recommended_action == "progress_ready_task":
        if not view.ready_task_ids:
            problems.append("recommended progressing a task while none is ready")
        elif (
            advice.target_task_id is not None
            and advice.target_task_id not in view.ready_task_ids
        ):
            problems.append(
                f"task {advice.target_task_id!r} is not ready; its prerequisites are unmet"
            )

    if advice.recommended_action != "escalate_configuration_review" and view.review_required:
        problems.append(
            "configuration review is outstanding, which outranks any other action"
        )

    if not problems:
        return advice

    fallback = OfflineAdvisor().advise(view, risk)
    fallback.source = advice.source
    fallback.degraded = True
    fallback.degraded_reason = (
        "model advice overruled by the rules engine: " + "; ".join(problems)
    )
    return fallback


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def llm_mode() -> str:
    """``LLM_MODE``, read the way the platform reads it: ``offline`` by default.

    Any other value selects the central gateway (same rule as UC-05's
    ``runtime.build_context``). UC-02 never inspects provider credentials —
    whether a route is usable is the gateway's business, and a failure there
    degrades to the deterministic advisor.
    """
    return os.environ.get("LLM_MODE", "offline").strip().lower() or "offline"


def llm_configured() -> bool:
    """Whether this environment asks for the central gateway.

    True only when ``LLM_MODE`` is not ``offline``, ``UC02_LLM_OFFLINE`` is not
    ``1`` and ``hr_onboarding_common.llm`` is importable. ``UC02_LLM_OFFLINE=1``
    forces the deterministic path regardless, which is how CI stays free and
    network-independent.
    """
    if os.environ.get("UC02_LLM_OFFLINE", "0") == "1":
        return False
    if llm_mode() == "offline":
        return False
    return COMMON_LLM_AVAILABLE


def fake_llm_requested() -> bool:
    """Whether ``UC02_LLM_FAKE=1`` asks for the deterministic fake model."""
    return os.environ.get("UC02_LLM_FAKE", "0") == "1"


def advisor_mode() -> str:
    """Which advisor this environment will produce: ``fake``, ``offline`` or ``litellm``.

    Reported in the demo and in ``integration_status`` so a reader never has
    to infer whether a model was involved.
    """
    if fake_llm_requested():
        return "fake"
    return "litellm" if llm_configured() else "offline"


def build_advisor(client: object | None = None, *, fake: bool | None = None) -> Advisor:
    """The advisor this environment can actually support.

    Order of preference:

    1. an explicit ``client`` — an injected ``LLMClient`` such as the
       orchestrator's ``AgentContext.llm``. Whoever built it already applied
       ``LLM_MODE`` (UC-05 pattern), so it is used as given;
    2. the deterministic fake, when ``fake=True`` or ``UC02_LLM_FAKE=1``;
    3. the central gateway, when :func:`llm_configured` (``LLM_MODE``
       online): ``hr_onboarding_common.llm.build_llm`` where the common
       package provides it, else ``LiteLLMGateway(get_settings())``. Model
       ids come from the common settings, never from this module;
    4. :class:`OfflineAdvisor`, the rule-based fallback.

    Never returns an object that pretends to have called a model: the fake
    reports ``source="fake_llm:fake/..."`` and the offline advisor reports
    ``source="offline"``.
    """
    if client is not None:
        return LiteLLMAdvisor(client)
    if fake or (fake is None and fake_llm_requested()):
        from onboarding_task_management.agent.fake_llm import build_fake_advisor

        return build_fake_advisor()
    if not llm_configured():
        return OfflineAdvisor()
    from hr_onboarding_common import llm as common_llm  # type: ignore[import-untyped]
    from hr_onboarding_common.config import get_settings  # type: ignore[import-untyped]

    factory = getattr(common_llm, "build_llm", None)
    if factory is not None:
        # The platform factory validates the online configuration and owns
        # provider routing (e.g. OpenRouter ids -> LiteLLM routes).
        return LiteLLMAdvisor(factory(get_settings()))
    return LiteLLMAdvisor(common_llm.LiteLLMGateway(get_settings()))


#: Longest local (non-gateway) error text copied into ``degraded_reason``.
_MAX_LOCAL_REASON = 200


def degraded_reason_for(exc: BaseException) -> str:
    """A degraded reason that cannot carry provider text.

    Gateway errors (``LLMError`` and subclasses) can echo provider responses
    or request content, so only their type and a fixed message are kept —
    the same rule as UC-05's ``SanitizedLLM``. Other exceptions come from
    UC-02's own code or a local fake; their text is kept, truncated, because
    it is what a developer needs to diagnose the fallback.
    """
    if isinstance(exc, LLMRefusal):
        return "LLMRefusal: the model declined the request; deterministic advice used"
    if isinstance(exc, LLMOutputError):
        return "LLMOutputError: the model output did not match the advice schema"
    if isinstance(exc, LLMError):
        return "LLMError: the language model is unavailable"
    text = str(exc)
    if len(text) > _MAX_LOCAL_REASON:
        text = text[:_MAX_LOCAL_REASON] + "…"
    return f"{type(exc).__name__}: {text}"


def advise_with_fallback(
    advisor: Advisor, view: ChecklistView, risk: JoiningRisk
) -> AgentAdvice:
    """Run ``advisor``, validate the result, and degrade honestly on failure.

    A model failure is never silent: the returned advice carries
    ``degraded=True`` and a sanitised reason (:func:`degraded_reason_for`),
    and the agent records both. A refusal is not retried here — the gateway
    contract says it must not be — it degrades like any other failure.
    """
    try:
        advice = advisor.advise(view, risk)
    except Exception as exc:  # noqa: BLE001 - any gateway failure must degrade, not crash
        fallback = OfflineAdvisor().advise(view, risk)
        fallback.source = getattr(advisor, "name", "unknown")
        fallback.degraded = True
        fallback.degraded_reason = degraded_reason_for(exc)
        return fallback
    return validate_advice(advice, view, risk)


__all__ = [
    "RECOMMENDABLE_ACTIONS",
    "SYSTEM_PREAMBLE",
    "ADVISE_TASK",
    "COMMON_LLM_AVAILABLE",
    "Advisor",
    "AgentAdvice",
    "Blocker",
    "LiteLLMAdvisor",
    "OfflineAdvisor",
    "advice_payload",
    "advise_with_fallback",
    "advisor_mode",
    "build_advisor",
    "default_tier",
    "degraded_reason_for",
    "derive_action",
    "derive_blockers",
    "fake_llm_requested",
    "input_block",
    "llm_configured",
    "llm_mode",
    "validate_advice",
]
