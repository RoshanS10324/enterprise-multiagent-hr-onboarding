"""The contract every LLM client implements, and the types around it.

Every UC-01 decision is deterministic Python; this package does not change
that. Clients: `FakeLlm` (offline, deterministic) and `GatewayLlm` (the
central hr_onboarding_common gateway, `gateway_client.py`).

An LLM here is **advisory only** -- it can
ask for a human to look at something, and it can draft text for a human
to read. It cannot approve, reject, write, or clear a review that the
deterministic rules raised.

Three rules hold for every implementation:

1. **Escalate-only authority.** A task declares its `authority` and the
   caller enforces it. `ADVISORY_ESCALATE_ONLY` means the response may
   add a review, never remove one.
2. **Structural redaction.** A request carries named inputs, not a
   free-text blob. `guard.py` rejects any input the task did not
   declare, and any `SecretStr`, before a prompt is built. Salary, bank,
   and loan data cannot reach a model by construction.
3. **Auditable.** Every call is logged by task name and outcome. The
   prompt text and the model's raw output are never written to the audit
   log, because either could echo an input.
"""

from enum import StrEnum
from hashlib import sha256
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class LlmAuthority(StrEnum):
    """What the caller is allowed to do with a response."""

    # May raise a review for a human; may never clear one.
    ADVISORY_ESCALATE_ONLY = "ADVISORY_ESCALATE_ONLY"
    # Produces text for a human to read. Never branches control flow.
    DRAFT_FOR_HUMAN = "DRAFT_FOR_HUMAN"


class LlmError(RuntimeError):
    """Base for every failure raised by this package."""


class LlmInputRefused(LlmError):
    """The request carried an input the task did not declare, or a secret.

    Carries the offending input NAMES only -- never their values, since
    this message may reach a log or an error page.
    """

    def __init__(self, task: str, offending: list[str]) -> None:
        self.task = task
        self.offending = sorted(offending)
        super().__init__(f"refused inputs for task {task!r}: {', '.join(self.offending)}")


class LlmUnavailable(LlmError):
    """The model could not be reached. Callers fall back to the
    deterministic path; a model outage never blocks a case."""


class LlmModelRefused(LlmError):
    """The model declined the request on content grounds (gateway
    `LLMRefusal`). Never retried; callers take the deterministic path,
    exactly as for an outage, but it is audited as a refusal."""

    def __init__(self, task: str) -> None:
        self.task = task
        super().__init__(f"model declined task {task!r}")


def audit_action_for(exc: LlmError) -> str:
    """The audit action for a failed advisory call.

    A refused INPUT is a wiring defect worth seeing separately from a model
    outage, and a model REFUSAL is neither. The entry still carries the task
    name only -- never the prompt or the model's output.
    """
    if isinstance(exc, LlmInputRefused):
        return "LLM_INPUT_REFUSED"
    if isinstance(exc, LlmModelRefused):
        return "LLM_MODEL_REFUSED"
    return "LLM_UNAVAILABLE"


def draft_fingerprint(text: Optional[str]) -> dict[str, object]:
    """What an audit entry may record about a model-drafted text: its
    length and a digest, so the draft can be matched to its audit entry
    later -- never the text itself."""
    body = text or ""
    return {"draft_chars": len(body), "draft_sha256": sha256(body.encode("utf-8")).hexdigest()}


class LlmTaskSpec(BaseModel):
    """One task the agent may ask a model to do, loaded from `brain/tasks/`."""

    model_config = ConfigDict(frozen=True)

    task: str
    # Prompt version from the task file. Recorded in the audit entry for
    # every call, so a decision a model influenced can be traced to the
    # exact wording in force at the time.
    version: int
    purpose: str
    authority: LlmAuthority
    inputs_allowed: frozenset[str]
    output_values: frozenset[str]
    prompt: str


class LlmRequest(BaseModel):
    """Named inputs for one call. Never a pre-built prompt string: the
    prompt is assembled from the task spec after the guard has run, so
    there is no path that smuggles an undeclared value into the text."""

    model_config = ConfigDict(frozen=True)

    task: str
    inputs: dict[str, str]


class LlmResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    task: str
    # The task spec version this response was produced under. Travels
    # with the response so the caller can audit provenance without
    # re-reading the task file.
    task_version: int
    # One of the task's declared output_values, or `text` for a draft task.
    label: Optional[str] = None
    text: Optional[str] = None
    # Deterministic for the fake; a real client would report the model's
    # own score. Only ever used to decide whether to escalate, never to
    # decide an outcome.
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    model: str


@runtime_checkable
class LlmClient(Protocol):
    def complete(self, request: LlmRequest) -> LlmResponse:
        """Run one task. Raises LlmInputRefused if the request carries an
        input the task did not declare, and LlmUnavailable on transport
        failure. Must be deterministic for a given request if the
        implementation is used in evals."""
        ...
