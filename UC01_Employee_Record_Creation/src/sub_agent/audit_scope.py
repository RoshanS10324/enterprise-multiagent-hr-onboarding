"""Which client an audit entry belongs to, when the entry itself cannot say.

The shared audit_events table requires a client_id on every row. Most UC-01
entries name a case, and the case names its client. Request-level entries
(START_GATE_EVALUATED, DUPLICATE_CHECK_EVALUATED, DOCUMENT_*) are written
before any case exists; for those, the entry point that is handling the
request declares its client for the duration of that request:

    with audit_client_scope(request.client_id):
        ...  # every audit append in here can be attributed

The scope is a context variable, reset when the block exits, so it never
leaks into another request. Nothing is ever guessed: an entry whose client
cannot be resolved is refused by the Supabase audit adapter.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_CURRENT_CLIENT: ContextVar[Optional[str]] = ContextVar("uc01_audit_client", default=None)


@contextmanager
def audit_client_scope(client_id: Optional[str]) -> Iterator[None]:
    token = _CURRENT_CLIENT.set(client_id)
    try:
        yield
    finally:
        _CURRENT_CLIENT.reset(token)


def current_audit_client() -> Optional[str]:
    return _CURRENT_CLIENT.get()
