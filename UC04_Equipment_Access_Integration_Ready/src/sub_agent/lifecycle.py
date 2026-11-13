"""Item state machine (BR-07). Approval, submission, ticket, provisioning, delivery, collection and completion are
different states and only legal moves are allowed (TC-30)."""

from sub_agent.states import ItemKind
from sub_agent.states import ItemState as S

_REAPPROVAL = (S.APPROVAL_PENDING, S.EXCEPTION_PENDING)  # a reassessment may send a pre-ticket item back for approval

ALLOWED: dict[S, frozenset[S]] = {
    S.DRAFT_PREPARED: frozenset({S.APPROVAL_PENDING, S.BLOCKED, S.CANCELLED}),
    S.APPROVAL_PENDING: frozenset({S.EXCEPTION_PENDING, S.APPROVAL_RECEIVED, S.EXCEPTION_RECEIVED, S.BLOCKED, S.CANCELLED}),
    S.EXCEPTION_PENDING: frozenset({S.APPROVAL_PENDING, S.EXCEPTION_RECEIVED, S.BLOCKED, S.CANCELLED}),
    S.APPROVAL_RECEIVED: frozenset({S.ELIGIBLE_FOR_SIMULATED_SUBMISSION, *_REAPPROVAL, S.BLOCKED, S.CANCELLED}),
    S.EXCEPTION_RECEIVED: frozenset({S.ELIGIBLE_FOR_SIMULATED_SUBMISSION, *_REAPPROVAL, S.BLOCKED, S.CANCELLED}),
    S.ELIGIBLE_FOR_SIMULATED_SUBMISSION: frozenset({S.MOCK_TICKET_CREATED, *_REAPPROVAL, S.BLOCKED, S.CANCELLED}),
    S.MOCK_TICKET_CREATED: frozenset({S.IT_IN_PROGRESS, S.REJECTED_CORRECTION_REQUIRED, S.BLOCKED, S.CANCELLED}),
    S.IT_IN_PROGRESS: frozenset({S.REJECTED_CORRECTION_REQUIRED, S.PROVISIONING_CONFIRMED, S.EQUIPMENT_DELIVERED,
                                 S.BLOCKED, S.CANCELLED}),
    S.REJECTED_CORRECTION_REQUIRED: frozenset({S.BLOCKED, S.CANCELLED}),
    S.PROVISIONING_CONFIRMED: frozenset({S.CLIENT_SITE_CLEARANCE_CONFIRMED, S.ITEM_COMPLETED, S.BLOCKED, S.CANCELLED}),
    S.EQUIPMENT_DELIVERED: frozenset({S.EQUIPMENT_COLLECTED, S.CLIENT_SITE_CLEARANCE_CONFIRMED, S.ITEM_COMPLETED,
                                      S.BLOCKED, S.CANCELLED}),
    S.EQUIPMENT_COLLECTED: frozenset({S.CLIENT_SITE_CLEARANCE_CONFIRMED, S.ITEM_COMPLETED, S.BLOCKED, S.CANCELLED}),
    S.CLIENT_SITE_CLEARANCE_CONFIRMED: frozenset({S.ITEM_COMPLETED, S.BLOCKED, S.CANCELLED}),
    S.ITEM_COMPLETED: frozenset({S.CANCELLED}),
    S.BLOCKED: frozenset({S.DRAFT_PREPARED, S.APPROVAL_PENDING, S.EXCEPTION_PENDING, S.APPROVAL_RECEIVED,
                          S.EXCEPTION_RECEIVED, S.ELIGIBLE_FOR_SIMULATED_SUBMISSION, S.CANCELLED}),
    S.CANCELLED: frozenset(),
}

PENDING_APPROVAL_STATES = {S.APPROVAL_PENDING, S.EXCEPTION_PENDING}
POST_TICKET_STATES = {
    S.MOCK_TICKET_CREATED, S.IT_IN_PROGRESS, S.REJECTED_CORRECTION_REQUIRED, S.PROVISIONING_CONFIRMED,
    S.EQUIPMENT_DELIVERED, S.EQUIPMENT_COLLECTED, S.CLIENT_SITE_CLEARANCE_CONFIRMED, S.ITEM_COMPLETED,
}
_PRE_TICKET_REAPPROVABLE = {S.APPROVAL_RECEIVED, S.EXCEPTION_RECEIVED, S.ELIGIBLE_FOR_SIMULATED_SUBMISSION}


class InvalidTransition(ValueError):
    pass


def transition(prev: S, new: S) -> None:
    """Validate one single-step move. Staying in the same state is allowed."""
    if new != prev and new not in ALLOWED[prev]:
        raise InvalidTransition(f"{prev} -> {new} is not a legal item transition")


def chain(kind: ItemKind, *, exception_item: bool, collection_required: bool, clearance_required: bool) -> list[S]:
    """The happy-path states this particular item must pass through, in order."""
    states = [S.DRAFT_PREPARED, S.APPROVAL_PENDING,
              S.EXCEPTION_RECEIVED if exception_item else S.APPROVAL_RECEIVED,
              S.ELIGIBLE_FOR_SIMULATED_SUBMISSION, S.MOCK_TICKET_CREATED, S.IT_IN_PROGRESS]
    if kind == ItemKind.ACCESS:
        states.append(S.PROVISIONING_CONFIRMED)
    else:
        states.append(S.EQUIPMENT_DELIVERED)
        if collection_required:
            states.append(S.EQUIPMENT_COLLECTED)
    if clearance_required:
        states.append(S.CLIENT_SITE_CLEARANCE_CONFIRMED)
    states.append(S.ITEM_COMPLETED)
    return states


def _position(cur: S, c: list[S]) -> int:
    if cur in c:
        return c.index(cur)
    if cur == S.BLOCKED:
        return 0
    if cur == S.EXCEPTION_PENDING:
        return 1
    if cur in (S.APPROVAL_RECEIVED, S.EXCEPTION_RECEIVED):
        return 2
    raise InvalidTransition(f"{cur} is not on this item's path")


def path(prev: S | None, target: S, *, kind: ItemKind, exception_item: bool = False, collection_required: bool = False,
         clearance_required: bool = False) -> list[S]:
    """The legal steps from the stored state to the state the evidence now supports (empty when unchanged).

    A new item starts at DRAFT_PREPARED. Every hop is validated against ALLOWED. A backward move (for example completed
    evidence that disappears) raises InvalidTransition, and the service then fails closed.
    """
    c = chain(kind, exception_item=exception_item, collection_required=collection_required,
              clearance_required=clearance_required)
    steps: list[S] = [S.DRAFT_PREPARED] if prev is None else []
    cur = prev if prev is not None else S.DRAFT_PREPARED
    if prev is not None and cur == target:
        return []
    if cur == S.CANCELLED:
        raise InvalidTransition("a cancelled item cannot move")

    if target in (S.BLOCKED, S.CANCELLED):
        hops = [target]
    elif target in _REAPPROVAL:
        if cur in _PRE_TICKET_REAPPROVABLE or cur in (S.DRAFT_PREPARED, S.BLOCKED, S.APPROVAL_PENDING, S.EXCEPTION_PENDING):
            hops = [S.APPROVAL_PENDING] if cur == S.DRAFT_PREPARED and target == S.EXCEPTION_PENDING else []
            hops.append(target)
        else:
            raise InvalidTransition(f"{cur} -> {target}: an item that already has a ticket cannot return to approval")
    elif target == S.REJECTED_CORRECTION_REQUIRED:
        hops = _forward(cur, S.MOCK_TICKET_CREATED, c) + [target] if cur not in (S.MOCK_TICKET_CREATED, S.IT_IN_PROGRESS) \
            else [target]
    else:
        if target not in c:
            raise InvalidTransition(f"{target} is not on this item's path")
        hops = _forward(cur, target, c)

    prior = cur
    for hop in hops:
        transition(prior, hop)
        prior = hop
    return steps + hops


def _forward(cur: S, target: S, c: list[S]) -> list[S]:
    start, goal = _position(cur, c), c.index(target)
    if goal < start:
        raise InvalidTransition(f"{cur} -> {target}: backward move")
    return c[start + 1:goal + 1]
