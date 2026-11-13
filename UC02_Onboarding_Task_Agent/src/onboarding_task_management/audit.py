"""In-memory append-only audit log (prototype seam).

Replace with ``hr_recruitment_common.platform.audit.AuditLog`` (JSONL, DSAR
redact) once ``uv sync`` can install the shared package. The method surface here
is intentionally minimal so that swap is mechanical.

CLAUDE.md rule 8: entries carry IDs, roles, counts and outcomes only — never
prompt text, names or contact details.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from uuid import uuid4

from onboarding_task_management.contracts import Actor, AuditEvent, Role

#: Keys that must never be written into an audit detail payload.
FORBIDDEN_DETAIL_KEYS = frozenset(
    {"name", "full_name", "email", "phone", "nationality", "passport", "address"}
)


#: Top-level ``detail`` keys the SHARED database's ``trg_audit_events_reject_pii`` rejects at insert
#: (``fn_audit_events_reject_pii``, from the consolidated schema). Kept separate from
#: ``FORBIDDEN_DETAIL_KEYS`` (which mirrors UC-02's own, unapplied migration) and enforced here so a
#: forbidden key fails in UC-02 with a clear error rather than as a database rejection mid-workflow.
SHARED_DB_FORBIDDEN_DETAIL_KEYS = frozenset(
    {"passport", "national_id", "ssn", "salary", "bank_account", "password", "token"}
)


class AuditLog:
    """Append-only, client-partitioned event log."""

    def __init__(self, sink: Callable[[AuditEvent], None] | None = None) -> None:
        #: Called with each new event BEFORE it joins the in-memory log. A durable
        #: store plugs in here; if it raises, the event is not recorded and the
        #: error reaches the caller, so an unsaved event is never reported saved.
        self.sink = sink
        self._events: list[AuditEvent] = []
        self._sequence = 0
        #: (client_id, onboarding_id) -> correlation id, so every later event
        #: for a case carries the caller's correlation id without each call
        #: site having to repeat it.
        self._correlation: dict[tuple[str, str], str] = {}

    def bind_correlation(
        self, client_id: str, onboarding_id: str, correlation_id: str | None
    ) -> None:
        if correlation_id:
            self._correlation[(client_id, onboarding_id)] = correlation_id

    def record(
        self,
        actor: Actor,
        action: str,
        outcome: str,
        *,
        onboarding_id: str | None = None,
        correlation_id: str | None = None,
        before_status: str | None = None,
        after_status: str | None = None,
        approver_id: str | None = None,
        previous_assignee_id: str | None = None,
        current_assignee_id: str | None = None,
        **detail: object,
    ) -> AuditEvent:
        leaked = (FORBIDDEN_DETAIL_KEYS | SHARED_DB_FORBIDDEN_DETAIL_KEYS) & set(detail)
        if leaked:
            raise ValueError(
                f"audit detail must not contain personal data keys: {sorted(leaked)}"
            )
        self._sequence += 1
        event = AuditEvent(
            event_id=self._next_event_id(),
            client_id=actor.client_id,
            onboarding_id=onboarding_id,
            actor_id=actor.actor_id,
            actor_role=actor.role,
            action=action,
            outcome=outcome,
            detail={k: str(v) for k, v in sorted(detail.items())},
            correlation_id=correlation_id
            or self._correlation.get((actor.client_id, onboarding_id or "")),
            task_id=str(detail["task_id"]) if detail.get("task_id") else None,
            before_status=before_status,
            after_status=after_status,
            approver_id=approver_id,
            previous_assignee_id=previous_assignee_id,
            current_assignee_id=current_assignee_id,
        )
        if self.sink is not None:
            self.sink(event)
        self._events.append(event)
        return event

    def restore(self, events: Iterable[AuditEvent]) -> int:
        """Re-seat events read back from durable storage. Idempotent.

        The id sequence advances past every restored id, otherwise the next event
        after a restart would reuse an id that is already stored. Restored events
        are not passed to ``sink``: they are already durable.
        """
        known = {e.event_id for e in self._events}
        added = 0
        for event in sorted(events, key=lambda e: (e.at, e.event_id)):
            if event.event_id in known:
                continue
            self._events.append(event.model_copy(deep=True))
            known.add(event.event_id)
            added += 1
            suffix = event.event_id.removeprefix("AUD-")
            if event.event_id.startswith("AUD-") and suffix.isdigit():
                self._sequence = max(self._sequence, int(suffix))
        return added

    def _next_event_id(self) -> str:
        """In-memory log: the familiar counter. With a durable sink: a time-ordered
        unique id, because several services or processes can share one store and a
        bare counter would reuse ids and violate append-only storage."""
        if self.sink is None:
            return f"AUD-{self._sequence:06d}"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        return f"AUD-{stamp}-{uuid4().hex[:8]}"

    def events_for(self, actor: Actor) -> list[AuditEvent]:
        """Events belonging to ``actor``'s client only.

        The scope is derived from the actor, never passed in, so a caller
        cannot widen it by supplying another client's id (finding H2). Copies
        are returned so a reader cannot rewrite the log in place.
        """
        return [
            e.model_copy(deep=True)
            for e in self._events
            if e.client_id == actor.client_id
        ]

    def actions_for(self, actor: Actor) -> list[str]:
        return [e.action for e in self.events_for(actor)]

    def all_events_privileged(self, *, reason: str) -> list[AuditEvent]:
        """Cross-client read. **Restricted** — platform/DSAR operations only.

        Deliberately named so that any cross-client access is obvious at the
        call site and in review. ``reason`` is mandatory and non-blank so the
        justification is recorded alongside the caller.
        """
        if not reason.strip():
            raise ValueError("privileged cross-client audit read requires a reason")
        return [e.model_copy(deep=True) for e in self._events]

    def __len__(self) -> int:
        """Total event count across all clients.

        A count, not content: used for assertions and capacity checks. It
        exposes no client's data.
        """
        return len(self._events)


SYSTEM_ACTOR = Actor(actor_id="system", role=Role.SYSTEM, client_id="")
