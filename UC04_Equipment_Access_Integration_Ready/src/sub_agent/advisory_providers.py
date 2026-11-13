"""Advisory providers behind one port. The advisor explains; the service decides.

`AdvisorPort` is what the agent and API call. `FakeLlmAdvisor` (offline, template-based) is the default and needs nothing. `LlmAdvisor` wraps
an injected `ModelPort`: a real model reached through the shared LiteLLM gateway, supplied by the layer that embeds UC-04. This module imports
no model library and reads no environment variable (a packaging safeguard forbids both), so provider choice is explicit and visible:
`build_advisor(provider, model=...)`, and every report names the provider that produced it.

Safety boundary:
* The model sees only facts the service already produced (status, counts, blocker codes, item states). Untrusted item notes are never
  part of the prompt.
* The model's text goes into `narrative` only, behind a banner, after the privacy guard. Every decision field of the report comes from the
  deterministic template advisor, so no model output can change a status, an approval, a ticket or an entitlement.
* A model failure never raises into the evaluation. The report says the narrative is unavailable and names the error class. It falls back to
  the template advisor only when that was configured explicitly, and then says so (`fallback_from`).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sub_agent.advisor import AdvisoryReport, FakeLlmAdvisor
from sub_agent.contracts import Uc04Input, Uc04Result
from sub_agent.guardrails import sanitize_free_text

MODEL_ADVISORY_BANNER = "[MODEL-GENERATED ADVISORY - NOT AN AUTHORITATIVE APPROVAL OR DECISION]"
MAX_NARRATIVE_CHARS = 1500
PROVIDERS = ("fake-llm", "litellm")

SYSTEM_PROMPT = (
    "You explain the outcome of a deterministic onboarding equipment and access check to an HR officer in plain language. "
    "You are advisory only. You never approve, grant, deny, select equipment or models, change a status or ticket, or contradict the "
    "facts given. Use only the facts in the input block. If something is not in the facts, say it is not known. Maximum 120 words."
)


@runtime_checkable
class AdvisorPort(Protocol):
    provider: str

    def generate_advisory(self, result: Uc04Result | None, input_data: Uc04Input | dict[str, Any] | None = None) -> AdvisoryReport: ...


@runtime_checkable
class ModelPort(Protocol):
    """A text model supplied from outside UC-04 (for example an adapter over the shared LiteLLM gateway). It raises on any failure."""

    name: str

    def complete(self, *, system: str, user: str, timeout_s: float) -> str: ...


class ModelNotConfigured(RuntimeError):
    """No model was supplied for the requested provider (credentials or configuration missing in the embedding layer)."""


class AdvisoryOutputRejected(RuntimeError):
    """The model answered, but the answer was empty, too long or not text."""


def facts_for(result: Uc04Result) -> str:
    """The only content a model sees: codes, states and counts produced by the service. No free text, no notes, no names."""
    items = ", ".join(f"{i.item_id}={i.state.value}" for i in result.items) or "none"
    blockers = ", ".join(sorted({b.code for b in result.blockers})) or "none"
    return "\n".join([
        f"overall_status: {result.overall_status.value}", f"items: {items}", f"blocker_codes: {blockers}",
        f"config_version: {result.config_version}",
    ])


class LlmAdvisor:
    provider = "litellm"

    def __init__(self, model: ModelPort, *, template: FakeLlmAdvisor | None = None, timeout_s: float = 20.0,
                 fallback_to_template: bool = False) -> None:
        self._model = model
        self._template = template or FakeLlmAdvisor()
        self._timeout_s = timeout_s
        self._fallback = fallback_to_template

    def generate_advisory(self, result: Uc04Result | None, input_data: Uc04Input | dict[str, Any] | None = None) -> AdvisoryReport:
        report = self._template.generate_advisory(result, input_data)  # every decision-bearing field comes from here
        if result is None:
            return report
        try:
            text = self._model.complete(system=SYSTEM_PROMPT, user=f"INPUT\n{facts_for(result)}", timeout_s=self._timeout_s)
            narrative = self._accept(text)
        except Exception as exc:  # noqa: BLE001 - any provider failure must leave the deterministic report intact
            return self._unavailable(report, type(exc).__name__)
        body = report.formatted_explanation.split("\n", 1)[-1]
        return report.model_copy(update={
            "is_simulated": False, "banner": MODEL_ADVISORY_BANNER, "provider": self.provider,
            "model": getattr(self._model, "name", None), "narrative": narrative,
            "formatted_explanation": f"{MODEL_ADVISORY_BANNER}\n{body}\n\nModel narrative: {narrative}"})

    @staticmethod
    def _accept(text: Any) -> str:
        if not isinstance(text, str) or not text.strip():
            raise AdvisoryOutputRejected("empty")
        text = " ".join(text.split())
        if len(text) > MAX_NARRATIVE_CHARS:
            raise AdvisoryOutputRejected("too long")
        cleaned = sanitize_free_text(text, max_len=MAX_NARRATIVE_CHARS)
        if not cleaned:
            raise AdvisoryOutputRejected("rejected by the privacy guard")
        return cleaned

    def _unavailable(self, report: AdvisoryReport, error: str) -> AdvisoryReport:
        if self._fallback:  # explicitly configured: visible, never silent
            return report.model_copy(update={"provider": FakeLlmAdvisor.provider, "fallback_from": self.provider, "advisory_error": error,
                                             "degraded": True, "degraded_reason": error})
        return report.model_copy(update={"provider": self.provider, "model": getattr(self._model, "name", None),
                                         "advisory_error": error, "degraded": True, "degraded_reason": error})


class UnavailableAdvisor:
    """The requested provider could not be set up. It says so on every report; nothing else is substituted."""

    def __init__(self, provider: str, error: str, template: FakeLlmAdvisor | None = None) -> None:
        self.provider = provider
        self._error = error
        self._template = template or FakeLlmAdvisor()

    def generate_advisory(self, result: Uc04Result | None, input_data: Uc04Input | dict[str, Any] | None = None) -> AdvisoryReport:
        return self._template.generate_advisory(result, input_data).model_copy(
            update={"provider": self.provider, "advisory_error": self._error, "degraded": True, "degraded_reason": self._error})


class _ExplicitFallback:
    """litellm requested with no model, and a template fallback explicitly allowed: the report says what happened."""

    provider = "fake-llm"

    def __init__(self, template: FakeLlmAdvisor) -> None:
        self._template = template

    def generate_advisory(self, result, input_data=None) -> AdvisoryReport:
        return self._template.generate_advisory(result, input_data).model_copy(
            update={"fallback_from": "litellm", "advisory_error": ModelNotConfigured.__name__, "degraded": True,
                    "degraded_reason": ModelNotConfigured.__name__})


def build_advisor(provider: str = "fake-llm", *, model: ModelPort | None = None, timeout_s: float = 20.0,
                  fallback_to_template: bool = False, template: FakeLlmAdvisor | None = None) -> AdvisorPort:
    """The one place a provider is chosen. Explicit, no environment access. An unknown provider is refused."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown advisory provider (known: {', '.join(PROVIDERS)})")
    if provider == "fake-llm":
        return template or FakeLlmAdvisor()
    if model is None:
        if fallback_to_template:
            return _ExplicitFallback(template or FakeLlmAdvisor())
        return UnavailableAdvisor("litellm", ModelNotConfigured.__name__, template)
    return LlmAdvisor(model, template=template, timeout_s=timeout_s, fallback_to_template=fallback_to_template)
