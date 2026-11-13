"""Append-only in-memory audit trail (GR-09, BR-15). Injected clock; a fixed field allowlist; reasons are sanitised."""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sub_agent.guardrails import assert_clean, sanitize_free_text
from sub_agent.ports import AuditSinkPort

Clock = Callable[[], datetime]
FIELDS = ("timestamp", "case_id", "request_id", "rule_id", "event_type", "previous_status", "new_status", "reason",
          "next_action", "owner")


def system_clock() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryAuditSink:
    """Reference `AuditSinkPort`: keeps (entry, context) pairs. Used by tests and as the model for a durable sink."""

    def __init__(self) -> None:
        self.records: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def write(self, entry: Any, context: Any) -> None:
        self.records.append((dict(entry), dict(context)))


class AuditLog:
    def __init__(self, clock: Clock = system_clock, file_path: Path | str | None = None, sink: AuditSinkPort | None = None,
                 defer_unattributed: bool = False):
        self._clock = clock
        # With a sink that needs a client (a database table), entries recorded before the verified client is known are held and written, in
        # order, once it is bound. Entries that never get a client stay in memory only (`unpersisted`); nothing is attributed to a made-up client.
        self.defer_unattributed = defer_unattributed
        self._pending: list[list[tuple[dict[str, Any], dict[str, Any]]]] = []  # parallel to _contexts
        self.unpersisted: list[dict[str, Any]] = []
        self.file_path = Path(file_path) if file_path else None
        self.events: list[dict[str, Any]] = []
        self.sink = sink  # optional second destination; the in-memory list and the JSONL file behave exactly as before
        self._contexts: list[dict[str, Any]] = []  # innermost scope last; empty outside a scoped call
        if self.file_path:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def context(self, **values: Any) -> Iterator[None]:
        """Scope for one call (an entry point wraps each call in one). Whatever is bound inside is discarded on exit, so a later call, or a
        call for another client, can never inherit it. The context is handed to the sink only; it is never added to an audit entry."""
        base = dict(self._contexts[-1]) if self._contexts else {}
        base.update({k: v for k, v in values.items() if v is not None})
        self._contexts.append(base)
        self._pending.append([])
        try:
            yield
        finally:
            self._contexts.pop()
            self.unpersisted.extend(entry for entry, _ in self._pending.pop())

    def bind_context(self, **values: Any) -> None:
        """Add values to the active scope (for example the verified client once the parent case is resolved). No-op outside a scope."""
        if self._contexts:
            self._contexts[-1].update({k: v for k, v in values.items() if v is not None})
            if self.sink is not None and self._contexts[-1].get("client_id") and self._pending and self._pending[-1]:
                held, self._pending[-1] = self._pending[-1], []
                for entry, held_context in held:
                    self.sink.write(dict(entry), {**held_context, **self._contexts[-1]})

    @property
    def current_context(self) -> dict[str, Any]:
        return dict(self._contexts[-1]) if self._contexts else {}

    def mark(self) -> int:
        return len(self.events)

    def since(self, mark: int, case_id: str) -> list[dict[str, Any]]:
        return [dict(e) for e in self.events[mark:] if e["case_id"] == case_id]

    def export_jsonl(self, path: Path | str) -> int:
        """Export in-memory events to a JSONL file for platform log review."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as f:
            for entry in self.events:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return len(self.events)

    def record(self, event_type: str, *, case_id: str | None, request_id: str | None = None, rule_id: str | None = None,
               previous_status: str | None = None, new_status: str | None = None, reason: str | None = None,
               next_action: str | None = None, owner: str | None = None) -> dict[str, Any]:
        entry = {
            "timestamp": self._clock().isoformat(), "case_id": case_id, "request_id": request_id, "rule_id": rule_id,
            "event_type": event_type,
            "previous_status": str(previous_status) if previous_status is not None else None,
            "new_status": str(new_status) if new_status is not None else None,
            "reason": sanitize_free_text(reason, 300), "next_action": sanitize_free_text(next_action, 300),
            "owner": owner,
        }
        assert_clean(entry, "audit")
        self.events.append(entry)
        if self.file_path:
            with self.file_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if self.sink is not None:
            context = self.current_context
            assert_clean(context, "audit-context")  # only checked when a sink exists, so behaviour without one is unchanged
            if self.defer_unattributed and self._pending and not context.get("client_id"):
                self._pending[-1].append((dict(entry), context))
            else:
                self.sink.write(dict(entry), context)
        return entry
