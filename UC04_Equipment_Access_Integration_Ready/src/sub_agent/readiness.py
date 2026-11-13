"""Case status and next actions. READY needs authoritative completion evidence for every required item (BR-17)."""

from sub_agent.contracts import Blocker, ItemResult, NextAction, Risk
from sub_agent.lifecycle import PENDING_APPROVAL_STATES
from sub_agent.states import CaseStatus, ItemState, Owner, Severity


def derive_status(*, cancelled: bool, blockers: list[Blocker], risks: list[Risk], items: list[ItemResult],
                  clearance_required: bool, clearance_ok: bool) -> tuple[CaseStatus, str]:
    active = [i for i in items if not i.superseded]
    if cancelled:
        return CaseStatus.CANCELLED, "The parent onboarding case is withdrawn or cancelled. No further processing."
    if any(b.severity == Severity.BLOCKING for b in blockers):
        codes = sorted({b.code for b in blockers if b.severity == Severity.BLOCKING})
        return CaseStatus.BLOCKED, f"Blocked: {', '.join(codes)}."
    if any(b.severity == Severity.REVIEW for b in blockers):
        codes = sorted({b.code for b in blockers if b.severity == Severity.REVIEW})
        return CaseStatus.NEEDS_HUMAN_REVIEW, f"A person must review: {', '.join(codes)}."
    if risks:
        codes = sorted({r.code for r in risks})
        return CaseStatus.AT_RISK, f"The joining date or a lead time is at risk: {', '.join(codes)}."
    if any(i.state in PENDING_APPROVAL_STATES for i in active):
        return CaseStatus.AWAITING_APPROVAL, "At least one request is waiting for a required approval."
    if active and all(i.state == ItemState.ITEM_COMPLETED for i in active) and (not clearance_required or clearance_ok):
        return CaseStatus.READY, "Every required item has authoritative completion evidence and nothing is outstanding."
    return CaseStatus.IN_PROGRESS, "Requests are progressing. Not every item has completion evidence yet."


def build_next_actions(items: list[ItemResult], blockers: list[Blocker], risks: list[Risk],
                       approver_owner: dict[str, str]) -> list[NextAction]:
    actions: list[NextAction] = []

    def add(action: str, owner: str, request_id: str | None) -> None:
        candidate = NextAction(action=action, owner=owner, request_id=request_id)
        if candidate not in actions:
            actions.append(candidate)

    for b in blockers:
        add(b.next_action, b.owner, b.request_id)
    for r in risks:
        add(r.next_action, r.owner, r.request_id)
    for i in items:
        if i.superseded or i.state in (ItemState.BLOCKED, ItemState.CANCELLED, ItemState.ITEM_COMPLETED,
                                       ItemState.REJECTED_CORRECTION_REQUIRED):
            continue
        owner = approver_owner.get(i.request_id, Owner.OWNER_CONFIG_MISSING)
        if i.state == ItemState.APPROVAL_PENDING:
            add("Obtain the required request approval.", owner, i.request_id)
        elif i.state == ItemState.EXCEPTION_PENDING:
            add("Obtain the exception approval for an out-of-matrix item.", owner, i.request_id)
        elif i.state == ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION:
            add("Submit the simulated request to the mock IT service desk.", Owner.UC04_SERVICE, i.request_id)
        elif i.state in (ItemState.MOCK_TICKET_CREATED, ItemState.IT_IN_PROGRESS):
            add("Wait for IT to complete the ticket and supply completion evidence.", Owner.IT_SERVICE_DESK, i.request_id)
        elif i.state in (ItemState.PROVISIONING_CONFIRMED, ItemState.EQUIPMENT_DELIVERED, ItemState.EQUIPMENT_COLLECTED,
                         ItemState.CLIENT_SITE_CLEARANCE_CONFIRMED):
            add("Provide the remaining completion evidence.", Owner.IT_SERVICE_DESK, i.request_id)
    return actions
