"""Idempotent delivery of one logical message through NotificationPort, backed by the durable
operation ledger (OperationRecord, OnboardingPort.get_operation/save_operation).

Every logical message has a stable `key` (e.g. "welcome:<client>:<draft id>"). The ledger gives it
one `operation_id`, created before the first attempt and sent as the message's idempotency_key on
every retry:

  ledger status   on the next deliver_once(key)
  (none)          new operation, PENDING, then the send
  PENDING         a previous attempt never recorded its outcome (crash, lost response): treated
                  like UNCERTAIN
  UNCERTAIN       provider.idempotent_delivery -> resend with the SAME idempotency key; the
                  provider deduplicates, so the recipient gets it at most once
                  otherwise -> DeliveryOutcomeUncertainError; never resent blindly
  SUCCEEDED       not sent again; the stored receipt is returned
  REJECTED        the provider refused it permanently; a later call is a fresh attempt
  RELEASED        an operator resolved it (see resolve_operation); a later call is a fresh attempt

This is at-most-once towards the recipient only when the provider honours idempotency keys; it is
not exactly-once. With a provider that does not, a lost response leaves the outcome unknown and HR
must check -- the ledger makes that visible instead of resending.
"""

from __future__ import annotations

from connectors.notifications.base import (DeliveryReceipt, NotificationChannel, NotificationDeliveryError,
                                           NotificationMessage, NotificationPort, NotificationRejectedError)
from hr_onboarding_common.contracts import now_utc
from sub_agent.models import OperationRecord, OperationStatus
from sub_agent.repository import OnboardingPort

KIND = "notification"
_FRESH = (OperationStatus.REJECTED, OperationStatus.RELEASED)


class DeliveryOutcomeUncertainError(RuntimeError):
    """An earlier attempt may have been delivered, and the provider cannot deduplicate a resend.
    Routed to HR; resolved with resolve_operation once someone has checked."""


def _receipt(op: OperationRecord) -> DeliveryReceipt:
    """The receipt of a delivery the ledger already holds (or an operator resolved as delivered)."""
    d = op.detail
    return DeliveryReceipt(delivery_ref=op.provider_ref or f"resolved:{op.key}", channel=NotificationChannel(d["channel"]),
                           recipient_id=d["recipient_id"], accepted_at=d.get("accepted_at") or op.updated_at,
                           simulated=d.get("simulated", False))


def deliver_once(port: OnboardingPort, notifier: NotificationPort, message: NotificationMessage, *,
                 key: str) -> DeliveryReceipt:
    op = port.get_operation(key)
    if op is not None and op.status == OperationStatus.SUCCEEDED:
        return _receipt(op)
    if op is None or op.status in _FRESH:
        op = OperationRecord(key=key, kind=KIND, client_id=message.client_id,
                             detail={"recipient_id": message.recipient_id, "kind": message.kind,
                                     "channel": message.channel.value})
    elif not notifier.idempotent_delivery:  # PENDING or UNCERTAIN
        raise DeliveryOutcomeUncertainError(
            f"an earlier attempt to deliver '{key}' may have succeeded and this provider cannot deduplicate a "
            f"resend; check with the recipient, then resolve operation '{key}'")
    op = port.save_operation(op.model_copy(update={"attempts": op.attempts + 1, "status": OperationStatus.PENDING}))
    try:
        receipt = notifier.send(message.model_copy(update={"idempotency_key": op.operation_id}))
    except NotificationRejectedError as e:
        port.save_operation(op.model_copy(update={"status": OperationStatus.REJECTED, "last_error": type(e).__name__}))
        raise
    except NotificationDeliveryError as e:
        port.save_operation(op.model_copy(update={"status": OperationStatus.UNCERTAIN, "last_error": type(e).__name__}))
        raise
    port.save_operation(op.model_copy(update={
        "status": OperationStatus.SUCCEEDED, "provider_ref": receipt.delivery_ref, "last_error": None,
        "detail": {**op.detail, "channel": receipt.channel.value, "recipient_id": receipt.recipient_id,
                   "accepted_at": receipt.accepted_at.isoformat(), "simulated": receipt.simulated}}))
    return receipt


def resolve_operation(port: OnboardingPort, *, key: str, client_id: str, outcome: OperationStatus,
                      provider_ref: str | None = None, resolved_by: str, reason: str | None = None) -> OperationRecord:
    """An operator's resolution of an UNCERTAIN/PENDING operation after checking the provider:
    SUCCEEDED (it did happen -- never redo it) or RELEASED (it did not happen, or was undone -- the
    next attempt starts fresh). Tenant-scoped: another client's key is reported as not found. The
    resolver, time and reason are kept in the operation's detail."""
    op = port.get_operation(key)
    if op is None or op.client_id != client_id:
        raise KeyError(key)
    if outcome not in (OperationStatus.SUCCEEDED, OperationStatus.RELEASED):
        raise ValueError("an operation can only be resolved as succeeded or released")
    if op.status not in (OperationStatus.PENDING, OperationStatus.UNCERTAIN):
        raise ValueError(f"operation '{key}' is {op.status}; only pending or uncertain operations are resolved")
    resolution = {"resolved_by": resolved_by, "resolved_at": now_utc().isoformat()}
    if reason:
        resolution["resolution_reason"] = reason.strip()[:500]
    return port.save_operation(op.model_copy(update={
        "status": outcome, "provider_ref": provider_ref or op.provider_ref, "detail": {**op.detail, **resolution}}))
