"""Append-only audit log (JSONL). Every tool call, LLM call, gate decision and exception.

Audit events must stay attributable and tenant-scoped: callers are expected to pass `client_id`
(and `actor`) through **details for every tenant-scoped event, since AuditLog itself does not know
which fields are sensitive. Never pass secrets, access tokens, or signature material as detail
values -- they are written verbatim to the JSONL file and to structured logs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.observability.logging import get_logger

log = get_logger("audit")


class AuditLog:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.events: list[dict[str, Any]] = []
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, *, actor: str | None = None, agent: str | None = None,
               subject_id: str | None = None, **details: Any) -> dict[str, Any]:
        entry = {"ts": now_utc().isoformat(), "event": event, "actor": actor, "agent": agent,
                 "subject_id": subject_id, **details}
        self.events.append(entry)
        log.info(event, extra={"audit": {k: v for k, v in entry.items() if k not in ("ts", "event") and v is not None}})
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def find(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]

    def redact_subject(self, subject_id: str) -> int:
        """DSAR support: blank out a subject's details in the in-memory log (file logs: rotate + rewrite)."""
        n = 0
        for e in self.events:
            if e.get("subject_id") == subject_id:
                for k in list(e):
                    if k not in {"ts", "event", "agent"}:
                        e[k] = "[redacted]"
                n += 1
        return n
