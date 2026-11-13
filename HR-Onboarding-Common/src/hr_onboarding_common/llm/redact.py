"""Redaction of provider text before it reaches an exception, a log line or an API response.

Provider error messages can echo credentials ("Incorrect API key provided: sk-..."), authorization
headers or parts of the request. Everything the gateway reports about a failure goes through
`redact`: configured secret values are replaced wherever they appear, anything shaped like a key or
bearer token is masked, and the text is cut short.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

# Credentials a LiteLLM route may read from the environment. Their values are never reported.
SECRET_ENV_VARS: tuple[str, ...] = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
                                    "AZURE_API_KEY")
MASK = "***"
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)bearer\s+[^\s\"',;}]+"), f"Bearer {MASK}"),
    (re.compile(r"(?i)\b(api[_-]?key|authorization|x-api-key|token)(\"?\s*[:=]\s*\"?)[^\s\"',;}]+"), rf"\1\2{MASK}"),
    (re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_\-]{6,}"), f"sk-{MASK}"),
)


def secret_values(environ: Mapping[str, str] | None = None, names: Iterable[str] = SECRET_ENV_VARS) -> list[str]:
    env = os.environ if environ is None else environ
    return [v for v in (env.get(n, "").strip() for n in names) if len(v) >= 8]


def redact(text: object, *, environ: Mapping[str, str] | None = None, limit: int = 300) -> str:
    """`text` with secrets masked, whitespace collapsed, and at most `limit` characters."""
    out = str(text)
    for value in secret_values(environ):
        out = out.replace(value, MASK)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    out = " ".join(out.split())
    return out if len(out) <= limit else out[: limit - 1] + "…"
