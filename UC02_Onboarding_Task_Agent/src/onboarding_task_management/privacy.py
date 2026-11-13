"""Free-text privacy filter for text that people type into UC-02 (HR reasons, delay and blocking reasons).

UC-02 stores identifiers, not personal data, and the shared database's audit trigger only inspects top-level ``detail`` keys.
Free text typed by a person is the one place personal data could still arrive, and it ends up in the case snapshot
(``plan_events`` reasons, task notes). :func:`redact_free_text` replaces the patterns most likely to be personal with
fixed markers before the text is stored. It is a safety net, not proof of absence: it cannot recognise a name.

Redacted: e-mail addresses, phone-number-like sequences, and long identifier-like tokens (8+ letters/digits containing a
digit, for example a passport or national id number). Task ids and other UC-02 identifiers contain hyphens or ``#`` and
are not touched.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w#-])\+?\d[\d\s().-]{7,}\d(?![\w#-])")
_IDLIKE = re.compile(r"(?<![\w#-])(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{8,}(?![\w#-])|(?<![\w#-])\d{8,}(?![\w#-])")

MAX_LENGTH = 500


def redact_free_text(text: str) -> str:
    """Return ``text`` with likely personal data replaced and length bounded. Idempotent."""
    out = _EMAIL.sub("[email removed]", text or "")
    out = _PHONE.sub("[number removed]", out)
    out = _IDLIKE.sub("[identifier removed]", out)
    return out.strip()[:MAX_LENGTH]


def contains_personal_pattern(text: str) -> bool:
    return redact_free_text(text) != (text or "").strip()[:MAX_LENGTH]


__all__ = ["MAX_LENGTH", "contains_personal_pattern", "redact_free_text"]
