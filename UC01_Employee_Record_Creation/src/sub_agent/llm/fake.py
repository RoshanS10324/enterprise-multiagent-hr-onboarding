"""A deterministic stand-in for a real model.

Calls nothing. Given the same request it always returns the same
response, which is what lets eval scenarios assert on LLM-influenced
behaviour without the result drifting between runs.

It is deliberately a little pessimistic: when a subject does not clearly
read as an acceptance, it returns UNSURE rather than guessing. Under
escalate-only authority, UNSURE means "a person looks at it", which is
the safe direction to be wrong in.

`fail_next` makes it raise LlmUnavailable once, so callers can exercise
the fallback path -- a model outage must never block a case.
"""

from hashlib import sha256

from .base import (
    LlmAuthority,
    LlmClient,
    LlmRequest,
    LlmResponse,
    LlmTaskSpec,
    LlmUnavailable,
)
from .guard import build_prompt

MODEL_NAME = "fake-deterministic-1"

# The lexicon the fake classifies on. Intentionally NOT imported from
# email_intake: the point of a second opinion is that it is independent,
# so the two can disagree and the disagreement escalates.
_DECLINE_MARKERS = (
    "decline",
    "declining",
    "reject",
    "not accept",
    "cannot accept",
    "can't accept",
    "unable to accept",
    "withdraw",
    "counter",
    "counter-offer",
    "negotiat",
    "reconsider",
    "step back",
    "other offer",
)
_ACCEPT_MARKERS = (
    "i accept",
    "accepted",
    "accepting",
    "happy to accept",
    "pleased to accept",
    "confirm my acceptance",
    "signed",
)


def _stable_confidence(prompt: str) -> float:
    """A reproducible pseudo-confidence in [0.50, 0.99].

    Derived from the prompt digest, so it is stable across runs,
    machines, and Python versions -- unlike anything seeded from
    `hash()`. It carries no real meaning; it exists so callers that
    threshold on confidence have something non-constant to test with.
    """
    digest = sha256(prompt.encode("utf-8")).digest()
    return 0.50 + (digest[0] / 255) * 0.49


class FakeLlm(LlmClient):
    """In-memory LLM. Records every call for assertions."""

    def __init__(self, tasks: dict[str, LlmTaskSpec]) -> None:
        self._tasks = dict(tasks)
        # Task names only. Never the prompts, so a test fixture that
        # prints this object cannot echo an input.
        self.calls: list[str] = []
        self.fail_next = False

    def _spec(self, task: str) -> LlmTaskSpec:
        spec = self._tasks.get(task)
        if spec is None:
            raise LlmUnavailable(f"no task spec loaded for {task!r}")
        return spec

    def complete(self, request: LlmRequest) -> LlmResponse:
        spec = self._spec(request.task)

        # Guard first: an undeclared or secret input must be refused
        # before any work happens, outage simulation included.
        prompt = build_prompt(spec, request.inputs)

        if self.fail_next:
            self.fail_next = False
            raise LlmUnavailable("simulated model outage")

        self.calls.append(spec.task)

        if spec.authority == LlmAuthority.DRAFT_FOR_HUMAN:
            return LlmResponse(
                task=spec.task,
                task_version=spec.version,
                text=self._draft(spec, request.inputs),
                confidence=_stable_confidence(prompt),
                model=MODEL_NAME,
            )

        return LlmResponse(
            task=spec.task,
            task_version=spec.version,
            label=self._classify(spec, request.inputs),
            confidence=_stable_confidence(prompt),
            model=MODEL_NAME,
        )

    def _classify(self, spec: LlmTaskSpec, inputs: dict[str, str]) -> str:
        if spec.task == "responsibilities_review":
            return self._review_responsibilities(spec, inputs)
        text = " ".join(inputs[k] for k in sorted(inputs)).lower()

        if any(marker in text for marker in _DECLINE_MARKERS):
            label = "POSSIBLE_DECLINE_OR_NEGOTIATION"
        elif any(marker in text for marker in _ACCEPT_MARKERS):
            label = "LOOKS_LIKE_ACCEPTANCE"
        else:
            # No clear signal. Say so rather than guessing: under
            # escalate-only authority this sends it to a human.
            label = "UNSURE"

        # Never return a label the task did not declare. A spec that
        # omits one of these degrades to UNSURE rather than producing an
        # output the caller has no branch for.
        return label if label in spec.output_values else "UNSURE"

    def _review_responsibilities(self, spec: LlmTaskSpec, inputs: dict[str, str]) -> str:
        """Deterministic stand-in: a title word (4+ letters) that also
        appears in the duties reads as a match; example wording reads as
        generic; anything else is UNSURE, which escalates."""
        duties = inputs.get("responsibilities", "").lower()
        title_words = {w for w in inputs.get("designation", "").lower().split() if len(w) >= 4}
        if any(marker in duties for marker in ("e.g", "example", "sample", "lorem")):
            label = "LOOKS_GENERIC_OR_EXAMPLE"
        elif title_words and any(w in duties for w in title_words):
            label = "MATCHES_DESIGNATION"
        else:
            label = "UNSURE"
        return label if label in spec.output_values else "UNSURE"

    def _draft(self, spec: LlmTaskSpec, inputs: dict[str, str]) -> str:
        """A templated summary. Renders only declared inputs, which by
        construction are field names and source names, never values."""
        lines = [f"{name.replace('_', ' ')}: {inputs[name]}" for name in sorted(inputs)]
        return (
            "For HR review. This is a draft summary and decides nothing; "
            "the approved onboarding form remains the source of truth.\n" + "\n".join(lines)
        )
