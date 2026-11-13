"""A deterministic fake LLM for UC-02.

Why this exists
---------------

:class:`~onboarding_task_management.agent.advisor.OfflineAdvisor` is a
**rule-based advisor**, not a fake model: it calls ``derive_action`` on the
rules engine directly, with no prompt, no payload and no response schema. Useful,
but it leaves the model-facing adapter — prompt assembly, the ``<input>`` block,
the structured-output round-trip, source tagging — executed by nothing.

:class:`FakeLLMClient` closes that gap. It satisfies the same ``LLMClient``
shape as ``hr_onboarding_common.llm.LiteLLMGateway`` (``model_for`` plus
``structured``) and is driven through the *same*
:class:`~onboarding_task_management.agent.advisor.LiteLLMAdvisor`. So every
line of adapter code a real gateway would run is run here too — only the
network call is replaced.

It is a **fake, and labelled as one everywhere**: the model id is
``fake/uc02-advisor-v1`` and the advice carries
``source="fake_llm:fake/uc02-advisor-v1"``. Nothing in this module may be read
as evidence that a real model works.

How the answers are produced
----------------------------

Scenarios are matched from the **prompt payload only** — the JSON inside the
``<input>`` block — never from the live ``ChecklistView``. That is deliberate:
if a scenario cannot be recognised from the payload, a real model could not
recognise it either, and the payload is missing something. The fake is
therefore a test of the prompt as much as of the agent.

Responses are canned templates filled with facts taken from that payload, so
output is byte-identical across runs and contains no invented task, owner,
date or requirement.

Failure injection
-----------------

``behaviour`` makes the unhappy paths reproducible:

=================  ========================================================
``"scripted"``     Scenario-matched, well-formed advice (default)
``"error"``        Raises, as a gateway timeout or exhausted retry chain would
``"invalid"``      Returns output that fails schema validation
``"hallucinate"``  Well-formed but unsupported by the record, so
                   ``validate_advice`` must overrule it
=================  ========================================================
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from onboarding_task_management.agent.advisor import (
    ADVISE_TASK,
    AgentAdvice,
    Blocker,
    LiteLLMAdvisor,
)

#: Model identifier reported by the fake. The ``fake/`` prefix is not
#: decoration: it appears in advice ``source`` and in the ``uc02.agent_run``
#: row, so no stored record can be mistaken for a real model call.
FAKE_MODEL = "fake/uc02-advisor-v1"

#: Scenario names, in the order :func:`classify` tests them.
SCENARIO_CONFIGURATION_REVIEW = "configuration_review"
SCENARIO_JOINING_RISK = "joining_date_risk"
SCENARIO_OVERDUE_FOLLOWUP = "overdue_followup"
SCENARIO_DOCUMENTS_INCOMPLETE = "documents_incomplete"
SCENARIO_DEPENDENT_BLOCKED = "dependent_blocked"
SCENARIO_READY_WORK = "ready_work"
SCENARIO_NOTHING_TO_DO = "nothing_to_do"

#: Template keys that represent document collection and verification. Matching
#: is on the client's configured key, so a client that names the step
#: differently simply falls through to the generic blocked-dependant scenario
#: rather than being described wrongly.
DOCUMENT_KEYS = frozenset({"collect_documents", "document_collection"})


class FakeLLMError(RuntimeError):
    """Raised by the fake in ``behaviour="error"``. Stands in for a gateway failure."""


@dataclass
class FakeCall:
    """One recorded call. Lets a test assert what the model was actually sent."""

    task: str
    tier: str
    model: str
    system: str
    payload: dict[str, Any]
    scenario: str = ""


def extract_input_block(user: str) -> dict[str, Any]:
    """Parse the ``<input>`` JSON out of the user turn.

    Mirrors ``hr_onboarding_common.llm.extract_input_block``. Returns ``{}``
    when the block is absent, which :meth:`FakeLLMClient.structured` treats as
    a malformed prompt rather than guessing.
    """
    match = re.search(r"<input>\n(.*?)\n</input>", user, re.DOTALL)
    if match is None:
        return {}
    return json.loads(match.group(1))


# ---------------------------------------------------------------------------
# Scenario classification — from the payload, nothing else
# ---------------------------------------------------------------------------


def _blocked_tasks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in payload.get("tasks", []) if t.get("blocked_by")]


def _document_blocked(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Tasks blocked specifically on a document-collection prerequisite."""
    return [
        task
        for task in _blocked_tasks(payload)
        if DOCUMENT_KEYS & set(task.get("blocked_by", []))
    ]


def classify(payload: dict[str, Any]) -> str:
    """Pick the scenario this payload describes.

    Precedence mirrors :func:`advisor.derive_action` on purpose. The fake is
    meant to behave like a *well-aligned* model: one that reaches the same
    conclusion the rules engine would. The adversarial case is a separate
    ``behaviour``, not the default, so a passing test means "the plumbing
    works", not "the model happened to agree".
    """
    if payload.get("review_required"):
        return SCENARIO_CONFIGURATION_REVIEW
    if payload.get("at_risk"):
        return SCENARIO_JOINING_RISK
    counts = payload.get("counts", {})
    if counts.get("overdue"):
        return SCENARIO_OVERDUE_FOLLOWUP
    if counts.get("ready"):
        return SCENARIO_READY_WORK
    if _document_blocked(payload):
        return SCENARIO_DOCUMENTS_INCOMPLETE
    if _blocked_tasks(payload):
        return SCENARIO_DEPENDENT_BLOCKED
    return SCENARIO_NOTHING_TO_DO


# ---------------------------------------------------------------------------
# Scripted responses
# ---------------------------------------------------------------------------


def _blockers_from_payload(payload: dict[str, Any], limit: int = 6) -> list[Blocker]:
    """Blockers phrased from the payload's own facts."""
    blockers: list[Blocker] = []
    for task in payload.get("tasks", []):
        if task.get("is_overdue"):
            blockers.append(
                Blocker(
                    task_id=task["task_id"],
                    template_key=task.get("template_key", ""),
                    reason=(
                        f"past its due date of {task.get('due_date')}; "
                        f"owner {task.get('owner')}"
                    ),
                )
            )
        elif task.get("blocked_by"):
            blockers.append(
                Blocker(
                    task_id=task["task_id"],
                    template_key=task.get("template_key", ""),
                    reason="waiting on " + ", ".join(task["blocked_by"]),
                )
            )
        if len(blockers) >= limit:
            break
    return blockers


def _first_ready(payload: dict[str, Any]) -> dict[str, Any] | None:
    for task in payload.get("tasks", []):
        if task.get("status") == "ready":
            return task
    return None


def _first_overdue(payload: dict[str, Any]) -> dict[str, Any] | None:
    for task in payload.get("tasks", []):
        if task.get("is_overdue"):
            return task
    return None


def respond(payload: dict[str, Any], scenario: str) -> AgentAdvice:
    """The canned answer for ``scenario``, filled from ``payload``.

    Every number and identifier in the prose comes from the payload. Nothing
    is invented: that is the property that makes the fake safe to show
    alongside the real adapter.
    """
    counts = payload.get("counts", {})
    blockers = _blockers_from_payload(payload)
    def advice(summary: str, action: str, target: str | None, rationale: str) -> AgentAdvice:
        """Fill the fields every scripted answer shares.

        A ``**kwargs`` dict was tempting here, but it types as
        ``dict[str, object]`` and defeats the schema check on every call --
        the point of the fake is to exercise the real contract, so the shared
        fields are passed explicitly.
        """
        return AgentAdvice(
            summary=summary,
            blockers=blockers,
            recommended_action=action,
            target_task_id=target,
            rationale=rationale,
            confidence=0.86,
            source=FAKE_MODEL,
        )

    if scenario == SCENARIO_CONFIGURATION_REVIEW:
        subjects = sorted(
            {
                issue["subject"]
                for issue in payload.get("issues", [])
                if issue.get("severity") != "informational"
            }
        )
        return advice(
            ("This plan cannot be progressed safely: the configuration "
                f"behind it has {len(subjects)} unresolved issue(s)."),
            "escalate_configuration_review",
            None,
            ("configuration review is outstanding for "
                + (", ".join(subjects) or "this client")
                + "; I will not guess a client or legal requirement"),
        )

    if scenario == SCENARIO_JOINING_RISK:
        reasons = payload.get("risk_reasons", [])
        return advice(
            (f"Day-1 readiness is at risk with {payload.get('days_to_joining')} "
                f"business day(s) to the {payload.get('joining_date')} joining date."),
            "prepare_followup",
            None,
            ("; ".join(reasons) or "the plan is flagged at risk"),
        )

    if scenario == SCENARIO_OVERDUE_FOLLOWUP:
        overdue = _first_overdue(payload)
        return advice(
            (f"{counts.get('overdue')} task(s) have passed their due date "
                f"as of {payload.get('as_of')}."),
            "prepare_followup",
            overdue["task_id"] if overdue else None,
            (f"the earliest overdue item is {overdue['template_key']!r}, "
                f"owned by {overdue['owner']}, due {overdue['due_date']}"
                if overdue
                else "overdue work is outstanding"),
        )

    if scenario == SCENARIO_READY_WORK:
        ready = _first_ready(payload)
        return advice(
            (f"{counts.get('ready')} task(s) are startable and "
                f"{counts.get('blocked')} are waiting on prerequisites."),
            "progress_ready_task",
            ready["task_id"] if ready else None,
            (f"{ready['template_key']!r} has every prerequisite met and is "
                f"owned by {ready['owner']}"
                if ready
                else "work is available to start"),
        )

    if scenario == SCENARIO_DOCUMENTS_INCOMPLETE:
        waiting = _document_blocked(payload)
        names = ", ".join(sorted(t["template_key"] for t in waiting))
        return advice(
            (f"Joining documents are not yet collected and verified, which "
                f"is holding up {len(waiting)} dependent task(s)."),
            "inspect_blockers",
            waiting[0]["task_id"],
            (f"{names} cannot start until document collection completes; "
                "the prerequisite is configured, not assumed"),
        )

    if scenario == SCENARIO_DEPENDENT_BLOCKED:
        blocked = _blocked_tasks(payload)
        first = blocked[0]
        return advice(
            (f"Nothing is startable: all {len(blocked)} open task(s) are "
                "waiting on a prerequisite."),
            "inspect_blockers",
            first["task_id"],
            (f"{first['template_key']!r} is waiting on "
                + ", ".join(first["blocked_by"])),
        )

    return advice(
        "No open work remains on this plan.",
        "no_action_required",
        None,
        (
            f"{counts.get('complete', 0)} task(s) complete, none ready, "
            "blocked or overdue"
        ),
    )


#: A well-formed recommendation the record does not support. Used by
#: ``behaviour="hallucinate"`` to prove ``validate_advice`` overrules a model.
HALLUCINATED_TASK_ID = "ONB-NOT-THIS-CLIENT#invented_task"


def hallucinated_response(payload: dict[str, Any]) -> AgentAdvice:
    """Confident, plausible and wrong — the failure mode that matters."""
    return AgentAdvice(
        summary="Everything looks fine; I have granted the outstanding access.",
        recommended_action="progress_ready_task",
        target_task_id=HALLUCINATED_TASK_ID,
        rationale="the candidate seems senior, so production access is justified",
        confidence=0.99,
        source=FAKE_MODEL,
    )


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


@dataclass
class FakeLLMClient:
    """Deterministic stand-in for ``LiteLLMGateway``.

    Implements the ``LLMClient`` surface the adapter uses: :meth:`model_for`
    and :meth:`structured`. No network, no credential, no randomness, no
    clock read.
    """

    behaviour: str = "scripted"
    model_name: str = FAKE_MODEL
    calls: list[FakeCall] = field(default_factory=list)
    #: Optional per-scenario override, so a test can script one answer.
    overrides: dict[str, AgentAdvice] = field(default_factory=dict)

    def model_for(self, tier: object) -> str:
        return self.model_name

    def structured(
        self,
        *,
        task: str,
        tier: object,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int = 8000,
    ) -> BaseModel:
        payload = extract_input_block(user)
        if not payload:
            raise FakeLLMError(
                "prompt carried no <input> block; the fake refuses to guess"
            )

        scenario = classify(payload)
        self.calls.append(
            FakeCall(
                task=task,
                tier=str(tier),
                model=self.model_name,
                system=system,
                payload=payload,
                scenario=scenario,
            )
        )

        if self.behaviour == "error":
            raise FakeLLMError("fake gateway failure: upstream timeout after 3 attempts")

        if self.behaviour == "invalid":
            # A real gateway validates before returning; this reproduces what
            # happens when the model's JSON does not fit the schema.
            try:
                return schema.model_validate(
                    {"summary": "…", "recommended_action": None, "confidence": 7.5}
                )
            except ValidationError as exc:
                raise FakeLLMError(f"schema validation failed: {exc.error_count()} error(s)") from exc

        if self.behaviour == "hallucinate":
            return hallucinated_response(payload)

        if scenario in self.overrides:
            return self.overrides[scenario]
        return respond(payload, scenario)

    # -- test helpers ------------------------------------------------------

    @property
    def last_scenario(self) -> str:
        return self.calls[-1].scenario if self.calls else ""

    @property
    def last_payload(self) -> dict[str, Any]:
        return self.calls[-1].payload if self.calls else {}


class FakeLLMAdvisor(LiteLLMAdvisor):
    """:class:`LiteLLMAdvisor` driven by :class:`FakeLLMClient`.

    Subclassed only to change the reported name, so ``source`` reads
    ``fake_llm:fake/uc02-advisor-v1`` and no run can be mistaken for a real
    model call. The ``advise`` implementation is inherited unchanged — which is
    the entire point.
    """

    name = "fake_llm"

    def __init__(self, client: FakeLLMClient | None = None, tier: object | None = None):
        super().__init__(client or FakeLLMClient(), tier)

    @property
    def fake_client(self) -> FakeLLMClient:
        return self.client  # type: ignore[return-value]


def build_fake_advisor(behaviour: str = "scripted") -> FakeLLMAdvisor:
    """Convenience constructor used by the demo and the tests."""
    return FakeLLMAdvisor(FakeLLMClient(behaviour=behaviour))


def register_fake_handler(llm: Any) -> Any:
    """Teach a common ``hr_onboarding_common.llm.FakeLLM`` to answer ``uc02.advise``.

    The orchestrator's offline ``AgentContext.llm`` is a ``FakeLLM`` with one
    handler per task (UC-05 pattern). Without a handler it raises ``LLMError``
    and UC-02 degrades to the deterministic advisor; with this one it returns
    the same scripted, payload-derived advice as :class:`FakeLLMClient`. The
    advice still passes through ``validate_advice``. Returns ``llm``.
    """

    def handler(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        payload = extract_input_block(user)
        if not payload:
            raise FakeLLMError("prompt carried no <input> block; the fake refuses to guess")
        return respond(payload, classify(payload))

    return llm.on(ADVISE_TASK, handler)


__all__ = [
    "DOCUMENT_KEYS",
    "FAKE_MODEL",
    "HALLUCINATED_TASK_ID",
    "SCENARIO_CONFIGURATION_REVIEW",
    "SCENARIO_DEPENDENT_BLOCKED",
    "SCENARIO_DOCUMENTS_INCOMPLETE",
    "SCENARIO_JOINING_RISK",
    "SCENARIO_NOTHING_TO_DO",
    "SCENARIO_OVERDUE_FOLLOWUP",
    "SCENARIO_READY_WORK",
    "FakeCall",
    "FakeLLMAdvisor",
    "FakeLLMClient",
    "FakeLLMError",
    "build_fake_advisor",
    "classify",
    "extract_input_block",
    "hallucinated_response",
    "register_fake_handler",
    "respond",
]
