"""Opt-in live check of the OpenRouter configuration. NOT part of the test suite: it makes real,
billable requests. Run it yourself after putting your key in HR-Onboarding-Common/.env:

    .venv/Scripts/python.exe scripts/openrouter_smoke_test.py                      one request, FAST tier
    .venv/Scripts/python.exe scripts/openrouter_smoke_test.py --tier all           one request per tier
    .venv/Scripts/python.exe scripts/openrouter_smoke_test.py --multi-turn         two turns, one conversation
    .venv/Scripts/python.exe scripts/openrouter_smoke_test.py --include-fallbacks  also each configured fallback
    .venv/Scripts/python.exe scripts/openrouter_smoke_test.py --check-only         validate, no request

It reads the .env (real environment variables win), validates the online configuration exactly as
the agents do at startup, then sends one minimal structured request per selected tier through the
production LiteLLMGateway -- the same routing, reasoning options, retries and Pydantic validation the
agents use. It reports the OpenRouter model id, the LiteLLM route, latency and token counts (reasoning
tokens included), or a sanitized failure. --multi-turn continues the conversation once, sending the
first answer's reasoning_details back, and reports how many came back -- never their content.
--include-fallbacks calls every model of each selected tier's chain on its own, so each fallback is
verified before it is relied on. The key is never printed (only whether it is set), nor is any
reasoning text.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace
from importlib.metadata import version

from pydantic import BaseModel

from hr_onboarding_common.config import Settings, load_environment
from hr_onboarding_common.llm import (LiteLLMGateway, LLMConfigurationError, LLMConversation, LLMError, ModelTier,
                                      llm_summary, validate_online_config)
from hr_onboarding_common.llm.callbacks import LLMEvent
from hr_onboarding_common.llm.factory import online_fallbacks

SYSTEM = "You are a connectivity check. Answer with the JSON requested."


class Ping(BaseModel):
    ok: bool
    echo: str


def _report_config(settings: Settings, tiers: list[ModelTier]) -> None:
    summary = llm_summary(settings)
    print(f"provider          : {summary['provider']}")
    print(f"base URL          : {summary['base_url']}")
    print(f"require_parameters: {summary['require_parameters']}")
    print(f"reasoning         : {summary['reasoning']}")
    for tier in tiers:
        t = tier.value
        fallbacks = " -> ".join(summary["fallback_routes"][t]) or "none"
        print(f"{t:<18}: model id {summary['models'][t]}  ->  LiteLLM route {summary['routes'][t]}  "
              f"(fallbacks: {fallbacks})")


def _ask(gateway: LiteLLMGateway, tier: ModelTier, user: str, conversation: LLMConversation | None = None,
         scope: tuple[str, ...] | None = None) -> Ping:
    return gateway.structured(task="smoke.ping", tier=tier, schema=Ping, max_tokens=300, system=SYSTEM, user=user,
                              conversation=conversation, scope=scope)


def _success_line(label: str, events: list[LLMEvent], reasoning_on: bool) -> str:
    success = next((e for e in reversed(events) if e.kind == "success"), None)
    if success is None:
        return f"[{label}] OK (no success event)"
    reasoning = success.reasoning_tokens
    note = "" if reasoning or not reasoning_on else "  (the provider reported no reasoning tokens)"
    return (f"[{label}] OK  route={success.model}  latency={success.latency_ms} ms  "
            f"tokens in/out={success.prompt_tokens}/{success.completion_tokens}  reasoning_tokens={reasoning}  "
            f"cost_usd={success.cost_usd}{note}")


def _check(settings: Settings, tier: ModelTier, label: str, multi_turn: bool) -> bool:
    events: list[LLMEvent] = []
    gateway = LiteLLMGateway(settings, callbacks=[events.append])
    started = time.perf_counter()
    scope = ("smoke-test", tier.value, label)
    conversation = LLMConversation(scope=scope) if multi_turn else None
    try:
        reply = _ask(gateway, tier, 'Return exactly {"ok": true, "echo": "pong"}.', conversation, scope)
        print("\n" + _success_line(label, events, settings.llm_reasoning_enabled))
        print(f"    structured reply validated: ok={reply.ok} echo={reply.echo!r}")
        if conversation is not None:
            details = conversation.history()[1].get("reasoning_details") or []
            types = sorted({str(d.get("type")) for d in details if isinstance(d, dict)})
            print(f"    reasoning_details returned: {len(details)} entr{'y' if len(details) == 1 else 'ies'} "
                  f"{types}  (content not shown; kept for the next turn)")
            _print_problems(events)
            events.clear()
            second = _ask(gateway, tier, 'Now return exactly {"ok": true, "echo": "second"}.', conversation, scope)
            print("    turn 2 " + _success_line(label, events, settings.llm_reasoning_enabled).split("] ", 1)[1])
            print(f"    turn 2 reply validated: ok={second.ok} echo={second.echo!r}  "
                  f"(sent with the earlier turn's reasoning_details unchanged)")
        _print_problems(events)
        return True
    except LLMError as e:  # already sanitized by the gateway: no key, no request text, no reasoning
        print(f"\n[{label}] FAILED after {time.perf_counter() - started:.1f}s: {type(e).__name__}: {e}")
        _print_problems(events)
        return False


def _print_problems(events: list[LLMEvent]) -> None:
    for event in events:
        if event.kind != "success":
            print(f"    {event.kind:<8} {event.model} attempt={event.attempt} error={event.error_type}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opt-in live OpenRouter check (billable requests).")
    parser.add_argument("--tier", choices=[t.value for t in ModelTier] + ["all"], default=ModelTier.FAST.value)
    parser.add_argument("--multi-turn", action="store_true",
                        help="continue the conversation once, sending reasoning_details back")
    parser.add_argument("--include-fallbacks", action="store_true",
                        help="also call each configured fallback model on its own")
    parser.add_argument("--check-only", action="store_true", help="validate the configuration; send nothing")
    args = parser.parse_args(argv)

    env_file = load_environment()
    print(f"env file          : {env_file or 'none found (environment variables only)'}")
    print(f"LLM_MODE          : {os.environ.get('LLM_MODE', 'offline')} (this check always goes online)")
    print(f"OPENROUTER_API_KEY: {'set' if os.environ.get('OPENROUTER_API_KEY', '').strip() else 'NOT SET'}")
    print(f"litellm           : {version('litellm')}")
    settings = Settings()
    try:
        validate_online_config(settings)
    except LLMConfigurationError as e:
        print(f"\nCONFIGURATION INVALID: {e}")
        return 2
    settings = online_fallbacks(settings)
    tiers = list(ModelTier) if args.tier == "all" else [ModelTier(args.tier)]
    _report_config(settings, tiers)
    if args.check_only:
        print("\nconfiguration valid; nothing sent (--check-only)")
        return 0

    failures = 0
    for tier in tiers:
        chain = [getattr(settings, f"model_{tier.value}"), *getattr(settings, f"fallbacks_{tier.value}")]
        targets = chain if args.include_fallbacks else chain[:1]
        for position, model in enumerate(targets):
            label = tier.value if position == 0 else f"{tier.value} fallback {position}"
            # Each model on its own: no fallback, so a failure is that model's.
            single = replace(settings, **{f"model_{tier.value}": model, f"fallbacks_{tier.value}": ()}) \
                if args.include_fallbacks else settings
            failures += not _check(single, tier, label, args.multi_turn)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
