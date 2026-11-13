"""From a configured model to the identifier LiteLLM is called with.

Two identifiers, kept apart on purpose:

  OpenRouter model id   what you configure (LLM_MODEL_*, OPENROUTER_MODEL): "<provider>/<model>" from
                        the OpenRouter catalog, e.g. "deepseek/deepseek-v4.1-flash"
  LiteLLM route         what the gateway passes to LiteLLM: "openrouter/<OpenRouter model id>"

They are not interchangeable: given "deepseek/deepseek-v4.1-flash" itself, LiteLLM would call its own
`deepseek` provider (DeepSeek's API, another key), not OpenRouter. Only this module maps one to the
other. With LLM_PROVIDER=litellm, configured models are LiteLLM routes and are used as given.
"""

from __future__ import annotations

from hr_onboarding_common.config import Settings

OPENROUTER, LITELLM = "openrouter", "litellm"
PROVIDERS = (OPENROUTER, LITELLM)
OPENROUTER_PREFIX = "openrouter/"


def openrouter_model_id(model: str) -> str:
    """The OpenRouter catalog id of `model`: an "openrouter/" prefix, if given, removed. A catalog id is
    always "<provider>/<model>", so OpenRouter's own ids ("openrouter/auto") are kept as they are."""
    model = model.strip()
    rest = model[len(OPENROUTER_PREFIX):]
    return rest if model.startswith(OPENROUTER_PREFIX) and "/" in rest else model


def litellm_route(model: str, settings: Settings) -> str:
    """The LiteLLM route for a configured model."""
    if settings.llm_provider == OPENROUTER:
        return OPENROUTER_PREFIX + openrouter_model_id(model)
    return model.strip()


def is_openrouter(route: str) -> bool:
    return route.startswith(OPENROUTER_PREFIX)
