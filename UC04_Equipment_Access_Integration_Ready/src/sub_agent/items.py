"""Evaluation of one requested item: allowlist, approvals, ticket, evidence and the item state the facts support."""

from dataclasses import dataclass, field
from datetime import date

from sub_agent.approvals import ApprovalOutcome, Resolution, assess, resolve
from sub_agent.calendar import subtract_working_days
from sub_agent.contracts import Blocker, RequestedItem, Uc04Input
from sub_agent.evidence import TicketFacts, is_authoritative, latest_by_request
from sub_agent.rules import ConfigGap, blocker, gap_for
from sub_agent.states import (ApprovalClass, ApprovalState, CollectionStatus, DeliveryStatus, EvidenceType, ItemKind,
                         ItemState, LeadKey, Owner, ProvisioningStatus, Severity, TicketStatus, WorkMode)


@dataclass
class ItemEval:
    item: RequestedItem
    request_id: str
    in_matrix: bool | None
    exception_required: bool
    std: Resolution
    exc: Resolution | None
    outcome: ApprovalOutcome
    target: ItemState
    collection_required: bool
    clearance_required: bool
    ticket: TicketFacts | None
    blockers: list[Blocker] = field(default_factory=list)
    provisioning_state: str | None = None
    delivery_state: str | None = None
    collection_state: str | None = None
    due_date: date | None = None
    lead_days: int | None = None
    lead_source: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    expected_delivery: date | None = None
    missed_slot_next: date | None = None
    missed_slot: bool = False
    delivery: object | None = None  # the latest IT delivery record for this request (equipment only)
    faulty_or_wrong: bool = False
    delivered_on: date | None = None  # when IT says it was delivered (EC-29); only set for a DELIVERED record


def _due(inp: Uc04Input, kind: ItemKind, gaps: list[ConfigGap]) -> tuple[date | None, int | None, str | None]:
    cfg = inp.config
    if gap_for(gaps, kind) or not cfg.work_calendar.is_usable or not cfg.lead_times.is_usable:
        return None, None, None
    key = LeadKey(kind.value)
    days = (cfg.lead_times.value or {}).get(key)
    if days is None:
        return None, None, None
    source = f"config:lead_times.{key.value}@{cfg.version}"
    if kind == ItemKind.EQUIPMENT and inp.work_mode != WorkMode.OFFICE and cfg.country_rules.is_usable:
        days += cfg.country_rules.value.courier_extra_days
        source += "+country_rules.courier_extra_days"
        local = cfg.country_rules.value.calendar()
        if local is not None:  # the country's own working days and holidays, from configuration
            source += "+country_rules.calendar"
            return subtract_working_days(inp.joining_date, days, local), days, source
    return subtract_working_days(inp.joining_date, days, cfg.work_calendar.value), days, source


def evaluate_item(inp: Uc04Input, item: RequestedItem, request_id: str, gaps: list[ConfigGap],
                  ticket: TicketFacts | None, clearance_ok: bool) -> ItemEval:
    cfg, kind = inp.config, item.item_kind
    scope_ok = not any(g.code == "CONFIG_SCOPE_MISMATCH" for g in gaps)
    matrix = cfg.role_access_matrix if kind == ItemKind.ACCESS else cfg.role_equipment_matrix
    in_matrix = None if (matrix.is_missing or not scope_ok) else item.item_id in (matrix.value or [])
    exception_required = in_matrix is False  # GR-03: out-of-matrix is an exception, never silently allowed

    unavailable = inp.unavailable_approver_roles
    std = resolve(ApprovalClass(kind.value), cfg, unavailable) if scope_ok else Resolution(config_missing=True)
    exc = None
    if exception_required:
        exc = resolve(ApprovalClass.EXCEPTION, cfg, unavailable) if scope_ok else Resolution(config_missing=True)

    outcome = assess(inp, request_id, item.item_id, std, exc)
    handover = cfg.handover_rules.value if cfg.handover_rules.is_usable else None
    collection_required = kind == ItemKind.EQUIPMENT and handover is not None and handover.mode.value == "COLLECT"
    due, lead_days, lead_source = _due(inp, kind, gaps)
    ev = ItemEval(item, request_id, in_matrix, exception_required, std, exc, outcome, ItemState.DRAFT_PREPARED,
                  collection_required, item.client_site, ticket, due_date=due, lead_days=lead_days, lead_source=lead_source)
    if kind == ItemKind.ACCESS:
        ev.provisioning_state = ProvisioningStatus.UNVERIFIED.value
    else:
        ev.delivery_state = DeliveryStatus.PENDING.value
        ev.collection_state = CollectionStatus.NOT_COLLECTED.value if collection_required else "NOT_REQUIRED"

    hard_block = gap_for(gaps, kind) is not None
    approvers_ready = cfg.approver_config.is_usable and scope_ok
    if approvers_ready and std.config_missing:
        ev.blockers.append(blocker(f"CONFIG_MISSING:approver_config.{kind.value}", Severity.BLOCKING,
                                   f"No approver is configured for {kind.value} requests.", Owner.HR_OFFICER,
                                   "Ask HR who approves this request class.", "BR-03", request_id))
        hard_block = True
    if approvers_ready and exc is not None and exc.config_missing:
        ev.blockers.append(blocker("CONFIG_MISSING:approver_config.EXCEPTION", Severity.BLOCKING,
                                   "No approver is configured for access or equipment exceptions.", Owner.HR_OFFICER,
                                   "Ask HR who approves exceptions.", "BR-03", request_id))
        hard_block = True
    for res, label in ((std, "standard"), (exc, "exception")):
        if res is not None and res.escalate_to_hr:
            ev.blockers.append(blocker("APPROVER_UNAVAILABLE_NO_DELEGATE", Severity.REVIEW,
                                       f"The {label} approver is unavailable and has no delegate.", Owner.HR_OFFICER,
                                       "Escalate to HR for an approver (EC-37).", "EC-37", request_id))

    held = kind == ItemKind.EQUIPMENT and inp.prior_equipment_held
    if held:
        ev.blockers.append(blocker("PRIOR_EQUIPMENT_HELD", Severity.REVIEW,
                                   "The employee still holds equipment from earlier employment.", Owner.HR_OFFICER,
                                   "Flag to HR and IT before issuing new equipment (EC-44).", "EC-44", request_id))
    rejected = ApprovalState.REJECTED in (outcome.standard, outcome.exception)
    if rejected:
        ev.blockers.append(blocker("APPROVAL_REJECTED", Severity.REVIEW, "An authorised approver rejected this request.",
                                   Owner.HIRING_MANAGER, "Review the rejection and raise a corrected request.", "BR-16",
                                   request_id))

    has_ticket = ticket is not None and ticket.ticket_id is not None
    approvals_ok = (outcome.standard == ApprovalState.RECEIVED
                    and (exc is None or outcome.exception == ApprovalState.RECEIVED))
    if hard_block or held or rejected:
        ev.target = ItemState.BLOCKED
    elif not approvals_ok:
        if has_ticket:
            ev.blockers.append(blocker("TICKET_WITHOUT_VALID_APPROVAL", Severity.REVIEW,
                                       "A ticket exists but no valid approval covers it.", Owner.HR_OFFICER,
                                       "Do not progress. Confirm the approval before relying on the ticket.", "GR-02",
                                       request_id))
            ev.target = ItemState.BLOCKED
        else:
            ev.target = (ItemState.APPROVAL_PENDING if outcome.standard != ApprovalState.RECEIVED
                         else ItemState.EXCEPTION_PENDING)
    else:
        ev.target = _progress(inp, ev, clearance_ok)
    return ev


def _review(ev: ItemEval, code: str, reason: str, owner: str, action: str, rule: str) -> None:
    ev.blockers.append(blocker(code, Severity.REVIEW, reason, owner, action, rule, ev.request_id))


def _progress(inp: Uc04Input, ev: ItemEval, clearance_ok: bool) -> ItemState:
    cfg, kind, rid, ticket = inp.config, ev.item.item_kind, ev.request_id, ev.ticket
    S = ItemState
    prov =latest_by_request(inp.provisioning_status, rid) if kind == ItemKind.ACCESS else None
    deliv = latest_by_request(inp.delivery_status, rid) if kind == ItemKind.EQUIPMENT else None
    coll = latest_by_request(inp.collection_status, rid) if kind == ItemKind.EQUIPMENT else None
    if deliv is not None:
        ev.delivery = deliv
        ev.expected_delivery = deliv.expected_date
        ev.delivery_state = deliv.status.value
        if deliv.status == DeliveryStatus.DELIVERED:
            ev.delivered_on = deliv.delivered_on
    if coll is not None and ev.collection_required:
        ev.collection_state = coll.status.value
        ev.missed_slot = coll.status == CollectionStatus.MISSED_SLOT
        ev.missed_slot_next = coll.next_slot

    if ticket is None or ticket.ticket_id is None:
        if prov or deliv or coll:
            _review(ev, "EVIDENCE_WITHOUT_TICKET", "Completion evidence arrived but no ticket exists for this request.",
                    Owner.IT_SERVICE_DESK, "Ignore the evidence until the request has a ticket.", "BR-17")
            ev.provisioning_state = ProvisioningStatus.UNVERIFIED.value if kind == ItemKind.ACCESS else None
            ev.delivery_state = DeliveryStatus.PENDING.value if kind == ItemKind.EQUIPMENT else None
        return S.ELIGIBLE_FOR_SIMULATED_SUBMISSION

    if ticket.latest == TicketStatus.REJECTED:
        _review(ev, "IT_REJECTED", f"IT rejected the request: {ticket.reason or 'no reason recorded, ask IT'}.",
                Owner.HIRING_MANAGER, "Raise a corrected request under a new request key. Do not retry the rejected one.",
                "EC-30", )
        if prov or deliv or coll:
            _review(ev, "EVIDENCE_AFTER_REJECTION", "Completion evidence was ignored because the ticket was rejected.",
                    Owner.IT_SERVICE_DESK, "Never treat a rejected request as active or delivered.", "BR-09")
        if kind == ItemKind.ACCESS:
            ev.provisioning_state = ProvisioningStatus.UNVERIFIED.value
        return S.REJECTED_CORRECTION_REQUIRED

    if ticket.latest != TicketStatus.COMPLETED:
        if prov or deliv or coll:
            _review(ev, "EVIDENCE_BEFORE_TICKET_COMPLETE", "Completion evidence arrived before the ticket was completed.",
                    Owner.IT_SERVICE_DESK, "Hold the evidence until IT completes the ticket.", "BR-17")
        return S.MOCK_TICKET_CREATED if ticket.latest == TicketStatus.CREATED else S.IT_IN_PROGRESS

    state = S.IT_IN_PROGRESS  # the ticket is complete, but a completed ticket is not proof of activation or delivery
    if kind == ItemKind.ACCESS:
        if prov is not None and prov.status == ProvisioningStatus.CONFIRMED_ACTIVE:
            if is_authoritative(prov.evidence_ref, prov.asserted_by_role, EvidenceType.PROVISIONING, cfg):
                state = S.PROVISIONING_CONFIRMED
                ev.provisioning_state = ProvisioningStatus.CONFIRMED_ACTIVE.value
                ev.evidence_refs.append(prov.evidence_ref)
            else:
                _review(ev, "EVIDENCE_NOT_AUTHORITATIVE", "Provisioning evidence lacks a stored reference or an approved role.",
                        Owner.IT_SERVICE_DESK, "Obtain authoritative provisioning evidence.", "GR-07")
        elif prov is not None and prov.status == ProvisioningStatus.FAILED:
            ev.provisioning_state = ProvisioningStatus.FAILED.value
            _review(ev, "PROVISIONING_FAILED", "Provisioning failed.", Owner.IT_SERVICE_DESK,
                    "IT to correct and report. Do not mark the access active.", "BR-17")
    else:
        if (deliv is not None and deliv.status == DeliveryStatus.DELIVERED and deliv.assigned_model and deliv.reported_model
                and deliv.assigned_model != deliv.reported_model):
            # IT says it assigned one model and the handover found another: that is a wrong model, never a delivery (EC-33)
            ev.faulty_or_wrong = True
            _review(ev, "EQUIPMENT_FAULTY_OR_WRONG", "The delivered model differs from the model IT assigned, so it is not recorded as assigned.",
                    Owner.IT_SERVICE_DESK, "Draft an exchange request. Do not assign this item (EC-33).", "BR-11")
        elif deliv is not None and deliv.status == DeliveryStatus.DELIVERED:
            if is_authoritative(deliv.evidence_ref, deliv.asserted_by_role, EvidenceType.DELIVERY, cfg):
                state = S.EQUIPMENT_DELIVERED
                ev.evidence_refs.append(deliv.evidence_ref)
            else:
                _review(ev, "EVIDENCE_NOT_AUTHORITATIVE", "Delivery evidence lacks a stored reference or an approved role.",
                        Owner.IT_SERVICE_DESK, "Obtain authoritative delivery evidence.", "GR-07")
        elif deliv is not None and deliv.status in (DeliveryStatus.FAULTY, DeliveryStatus.WRONG_MODEL):
            ev.faulty_or_wrong = True
            _review(ev, "EQUIPMENT_FAULTY_OR_WRONG", "The equipment is faulty or the wrong model and is not assigned.",
                    Owner.IT_SERVICE_DESK, "Draft an exchange request. Do not assign this item (EC-33).", "BR-11")
        if state == S.EQUIPMENT_DELIVERED and ev.collection_required:
            if coll is not None and coll.status == CollectionStatus.COLLECTED:
                if is_authoritative(coll.evidence_ref, coll.asserted_by_role, EvidenceType.COLLECTION, cfg):
                    state = S.EQUIPMENT_COLLECTED
                    ev.evidence_refs.append(coll.evidence_ref)
                else:
                    _review(ev, "EVIDENCE_NOT_AUTHORITATIVE",
                            "Collection evidence lacks a stored reference or an approved role.", Owner.IT_SERVICE_DESK,
                            "Obtain authoritative collection evidence.", "GR-07")

    done = state == S.PROVISIONING_CONFIRMED or (
        state == S.EQUIPMENT_DELIVERED and not ev.collection_required) or state == S.EQUIPMENT_COLLECTED
    if done and ev.item.client_site:
        return S.ITEM_COMPLETED if clearance_ok else state
    return S.ITEM_COMPLETED if done else state
