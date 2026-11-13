"""Joining-date, lead-time, client-site and workaround logic (DL-07, DL-13, BR-10, BR-12, BR-19, BR-20).

Workarounds are proposals for a human decision. Nothing is ordered, assigned, sent or changed.
"""

from dataclasses import dataclass, field

from sub_agent.calendar import is_working_day, next_working_day, subtract_working_days
from sub_agent.contracts import Blocker, ClientSiteClearance, Proposal, RemotePlan, Risk, Uc04Input
from sub_agent.evidence import is_authoritative
from sub_agent.guardrails import sanitize_free_text
from sub_agent.items import ItemEval
from sub_agent.lifecycle import PENDING_APPROVAL_STATES
from sub_agent.rules import ConfigGap, blocker
from sub_agent.states import ClearanceStatus, EvidenceType, ItemKind, ItemState, LeadKey, Owner, Severity, WorkMode

_PRE_TICKET = {ItemState.APPROVAL_PENDING, ItemState.EXCEPTION_PENDING, ItemState.APPROVAL_RECEIVED,
               ItemState.EXCEPTION_RECEIVED, ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION}
_FINISHED = {ItemState.ITEM_COMPLETED, ItemState.CANCELLED}


def _risk(code: str, reason: str, owner: str, next_action: str, rule: str, request_id: str | None = None) -> Risk:
    return Risk(code=code, request_id=request_id, reason=reason, owner=owner, next_action=next_action, rule_id=rule)


def _draft(kind: str, audience: str, subject: str, body: str, rid: str | None, approver: str = Owner.HR_OFFICER) -> Proposal:
    """A message DRAFT for a person to approve and send. UC-04 never sends it. Content: ids, dates and codes only; any IT text is quoted as data."""
    return Proposal(proposal_type=f"MESSAGE_DRAFT_{kind}", request_id=rid, requires_approval_by=approver,
                    description=f"Draft {audience.lower()} message for approval. Nothing is sent.",
                    payload={"audience": audience, "subject": subject, "body": body, "status": "DRAFT_NOT_SENT"})


def _it_request(kind: str, rid: str | None, description: str, payload: dict, approver: str = Owner.HR_OFFICER) -> Proposal:
    """An IT request DRAFT. IT decides and executes; UC-04 only prepares and later records what IT confirms."""
    return Proposal(proposal_type=kind, request_id=rid, requires_approval_by=approver, description=description,
                    payload={**payload, "status": "DRAFT_NOT_SENT"})


@dataclass
class ClientSiteAssessment:
    status: ClearanceStatus | None = None
    confirmed: bool = False
    blockers: list[Blocker] = field(default_factory=list)
    risks: list[Risk] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)


def assess_client_site(inp: Uc04Input, gaps: list[ConfigGap]) -> ClientSiteAssessment:
    out = ClientSiteAssessment()
    cfg = inp.config
    if not inp.client_site_required:
        return out
    clearance = inp.client_site_clearance or ClientSiteClearance(status=ClearanceStatus.PENDING)
    out.status = clearance.status
    if not cfg.client_site_rules.is_usable:
        return out  # the missing configuration is already a blocking gap (BR-03)
    rules = cfg.client_site_rules.value
    owner = rules.owner_role

    requested = {i.item_id for i in inp.requested_access + inp.requested_equipment}
    for needed in rules.required_items:
        if needed not in requested:
            out.blockers.append(blocker("CLIENT_SITE_ITEM_MISSING", Severity.REVIEW,
                                        f"Configured client-site item '{needed}' has not been requested.", owner,
                                        "Raise the client-site request through the account manager.", "DL-13"))

    if clearance.status == ClearanceStatus.CONFIRMED:
        if cfg.evidence_roles.is_usable and is_authoritative(clearance.evidence_ref, clearance.asserted_by_role,
                                                             EvidenceType.CLIENT_SITE_CLEARANCE, cfg):
            out.confirmed = True
        else:
            out.blockers.append(blocker("EVIDENCE_NOT_AUTHORITATIVE", Severity.REVIEW,
                                        "Client-site clearance lacks a stored reference or an approved role.", owner,
                                        "Obtain authoritative clearance evidence.", "GR-07"))
        return out
    if clearance.status == ClearanceStatus.DENIED:
        out.blockers.append(blocker("CLIENT_SITE_CLEARANCE_DENIED", Severity.REVIEW, "Client-site clearance was denied.",
                                    owner, "HR and the account manager decide an office start or another deployment.",
                                    "EC-34"))
        return out

    lead = (cfg.lead_times.value or {}).get(LeadKey.CLIENT_SITE_CLEARANCE) if cfg.lead_times.is_usable else None
    if lead is None or not cfg.work_calendar.is_usable or any(g.code == "CONFIG_INVALID:lead_times.CLIENT_SITE_CLEARANCE"
                                                              for g in gaps):
        return out
    target = inp.deployment_date or inp.joining_date  # the date the client site needs the person
    clearance_due = subtract_working_days(target, lead, cfg.work_calendar.value)
    late = (clearance.expected_date is not None and clearance.expected_date > target) \
        or inp.as_of_date > clearance_due
    if late:
        reason = "Client-site clearance cannot finish before the joining date."
        action = "Flag at plan approval. HR and the account manager decide an office start or a later deployment."
        if rules.block_on_late_clearance:
            out.blockers.append(blocker("CLIENT_SITE_CLEARANCE_LATE", Severity.BLOCKING, reason, owner, action, "EC-34"))
        else:
            out.risks.append(_risk("CLIENT_SITE_CLEARANCE_LATE", reason, owner, action, "EC-34"))
        out.proposals.append(Proposal(proposal_type="OFFICE_START_OR_LATER_DEPLOYMENT",
                                      description="Propose an office start or a later client deployment date. HR decides with the account manager.",
                                      requires_approval_by=Owner.HR_OFFICER,
                                      payload={"joining_date": inp.joining_date.isoformat(), "deployment_date": target.isoformat(),
                                               "clearance_due": clearance_due.isoformat(), "account_manager_role": owner,
                                               "status": "DRAFT_NOT_DECIDED"}))
    return out


def item_risks(inp: Uc04Input, ev: ItemEval) -> tuple[list[Risk], list[Proposal]]:
    risks: list[Risk] = []
    proposals: list[Proposal] = []
    state, rid = ev.target, ev.request_id
    if state == ItemState.REJECTED_CORRECTION_REQUIRED:  # EC-30: tell the hiring manager what IT said and ask for corrected details
        reason = sanitize_free_text((ev.ticket.reason if ev.ticket else None) or "no reason recorded", max_len=200)
        proposals.append(_draft(
            "MANAGER_REJECTION", "MANAGER", f"IT rejected request {rid}",
            f"IT rejected request {rid}. Recorded reason, quoted as data: \"{reason}\". Please send corrected details. "
            f"A corrected request will be raised under a new request key; the rejected one is not retried.", rid))
    if state in _FINISHED or state == ItemState.BLOCKED:
        return risks, proposals

    at_risk = False
    if ev.due_date is not None and inp.as_of_date > ev.due_date:
        if state in PENDING_APPROVAL_STATES:
            res = ev.std if state == ItemState.APPROVAL_PENDING else ev.exc
            risks.append(_risk("APPROVAL_STALLED", "The approval is outstanding past the due date for this request.",
                               res.owner if res else Owner.OWNER_CONFIG_MISSING,
                               "Escalate to the HR officer, then the HR operations lead after one working day.", "GR-12", rid))
            at_risk = True
        elif state in _PRE_TICKET:
            risks.append(_risk("REQUEST_NOT_RAISED_BY_DUE_DATE", "The request was not raised in time for the configured lead time.",
                               Owner.HR_OFFICER, "Re-plan and decide a workaround with the hiring manager.", "DL-07", rid))
            at_risk = True
    # EC-29: a delivery IT has already recorded after the joining date counts like an expected late one (a delivery ON the joining date is on time).
    delivered_late = ev.delivered_on is not None and ev.delivered_on > inp.joining_date
    expected_late = ev.expected_delivery is not None and ev.expected_delivery > inp.joining_date
    if delivered_late or expected_late:
        late_date = ev.delivered_on if delivered_late else ev.expected_delivery
        risks.append(_risk("EQUIPMENT_DELAYED_BEYOND_JOINING",
                           "Delivery happened after the joining date." if delivered_late else "Delivery is expected after the joining date.",
                           Owner.HR_OFFICER, "Flag to the hiring manager and HR. Draft any employee message for HR approval.",
                           "EC-29", rid))
        at_risk = True
        when, join = late_date.isoformat(), inp.joining_date.isoformat()
        verb = "was delivered on" if delivered_late else "is currently expected on"
        proposals.append(_draft("EMPLOYEE_DELAY", "EMPLOYEE", "Your laptop delivery date",
                                f"Your laptop (request {rid}) {verb} {when}, after your joining date {join}. IT is responsible for the "
                                f"laptop. HR will tell you about any interim arrangement once IT has confirmed one. Nothing is confirmed yet.", rid))
        proposals.append(_draft("MANAGER_DELAY", "MANAGER", ("Laptop was delivered after the joining date" if delivered_late
                                                             else "Laptop delivery expected after the joining date"),
                                f"The laptop for request {rid} {verb.replace('is currently expected on', 'is expected on')} {when}, after the joining date {join}. "
                                f"HR has been told. A loan device is only a proposal until IT confirms one is available.", rid))
    if inp.as_of_date >= inp.joining_date:
        risks.append(_risk("JOINING_DATE_REACHED_INCOMPLETE", "The joining date is reached and this item is not complete.",
                           Owner.HR_OFFICER, "Escalate to HR now.", "GR-12", rid))
        at_risk = True
    if ev.missed_slot:
        reserve = inp.config.handover_rules.value.keep_reserved_on_missed_slot if inp.config.handover_rules.is_usable else None
        note = ("Keep the device reserved." if reserve else "Do not hold the device.") if reserve is not None else ""
        slot = f" Offer the next slot {ev.missed_slot_next.isoformat()}." if ev.missed_slot_next else ""
        risks.append(_risk("COLLECTION_SLOT_MISSED", "The employee missed the booked collection slot.", Owner.HR_OFFICER,
                           f"Draft a reminder to the employee and manager for HR approval.{slot} {note}".strip(), "EC-32", rid))
        offered = ev.missed_slot_next.isoformat() if ev.missed_slot_next else None
        text = f"offered slot {offered}, to be confirmed by IT" if offered else "no new slot yet; IT will confirm one"
        proposals.append(_draft("EMPLOYEE_COLLECTION_REMINDER", "EMPLOYEE", "Please collect your laptop",
                                f"The booked collection slot for request {rid} was missed. Next step: {text}. IT confirms the slot.", rid))
        proposals.append(_draft("MANAGER_COLLECTION_REMINDER", "MANAGER", "Laptop collection was missed",
                                f"The employee did not collect the laptop for request {rid} at the booked time. Next step: {text}.", rid))
        proposals.append(_it_request("REQUEST_COLLECTION_SLOT", rid, "Ask IT for the next available collection slot. IT confirms; UC-04 records only a confirmed slot.",
                                     {"request_id": rid, "offered_slot": offered, "confirmed_slot": None}))
        if reserve:
            proposals.append(_it_request("RETAIN_RESERVATION_REQUEST", rid, "Ask IT to keep the device reserved until the new slot. IT executes.",
                                         {"request_id": rid, "retain_until": offered, "confirmed": False}))

    if at_risk and ev.item.item_kind == ItemKind.EQUIPMENT:
        proposals.append(Proposal(proposal_type="LOAN_DEVICE", request_id=rid,
                                  description="Propose a loan device for the joining date. Nothing is ordered or assigned.",
                                  requires_approval_by=Owner.HR_OFFICER,
                                  payload={"it_request": "LOAN_DEVICE_REQUEST", "needed_by": inp.joining_date.isoformat(),
                                           "availability_confirmed": False, "status": "DRAFT_NOT_SENT"}))
    if ev.faulty_or_wrong:
        d = ev.delivery
        note = sanitize_free_text(d.fault_note, max_len=200) if d is not None and d.fault_note else None
        fields = {"employee_ref": inp.employee_ref, "case_id": inp.case_id, "request_id": rid, "expected_item": ev.item.item_id,
                  "reported_status": ev.delivery_state, "asset_ref": getattr(d, "asset_ref", None),
                  "assigned_model": getattr(d, "assigned_model", None), "reported_model": getattr(d, "reported_model", None),
                  "fault_note": note, "requested_action": "EXCHANGE", "replacement_confirmed": False}
        proposals.append(Proposal(proposal_type="EXCHANGE_REQUEST_DRAFT", request_id=rid,
                                  description="Draft an exchange request. The faulty or wrong item stays unassigned. IT performs the exchange.",
                                  requires_approval_by=Owner.HIRING_MANAGER,
                                  payload={**{k: v for k, v in fields.items() if v is not None}, "status": "DRAFT_NOT_SENT"}))
    rules = inp.config.country_rules
    if (ev.item.item_kind == ItemKind.EQUIPMENT and inp.work_mode != WorkMode.OFFICE and rules.is_usable):  # EC-36
        proposals.append(_it_request("COURIER_REQUEST_DRAFT", rid, "Ask IT to arrange courier delivery to the employee's approved location. IT books it.",
                                     {"request_id": rid, "country_code": rules.value.country_code, "courier_extra_days": rules.value.courier_extra_days,
                                      "needed_by": ev.due_date.isoformat() if ev.due_date else None, "work_location": inp.work_location,
                                      "booking_confirmed": False}))
    return risks, proposals


def case_risks(inp: Uc04Input) -> tuple[list[Risk], list[Proposal], list[Blocker]]:
    risks: list[Risk] = []
    proposals: list[Proposal] = []
    blockers: list[Blocker] = []
    cal = inp.config.work_calendar
    local = inp.config.country_rules.value.calendar() if (inp.work_mode != WorkMode.OFFICE and inp.config.country_rules.is_usable) else None
    cal_value = local or cal.value
    if (cal.is_usable or local) and not is_working_day(inp.joining_date, cal_value):
        suggestion = next_working_day(inp.joining_date, cal_value)
        risks.append(_risk("JOINING_DATE_NON_WORKING_DAY", "The joining date is a weekend or public holiday.",
                           Owner.HR_OFFICER, "Flag to HR. The joining date is never changed automatically.", "EC-13"))
        proposals.append(Proposal(proposal_type="NEXT_WORKING_DAY_SUGGESTION",
                                  description=f"Suggest {suggestion.isoformat()} as the joining date. HR decides.",
                                  requires_approval_by=Owner.HR_OFFICER))
    if inp.internal_transfer and inp.transfer is None:
        blockers.append(blocker("INTERNAL_TRANSFER_REVIEW", Severity.REVIEW,
                                "Internal transfer: existing access and equipment need review for the new role.",
                                Owner.HR_OFFICER, "HR and IT review what the previous role still holds (EC-42).", "EC-42"))
    return risks, proposals, blockers


def remote_plan(inp: Uc04Input) -> RemotePlan | None:
    if inp.work_mode == WorkMode.OFFICE:
        return None
    rules = inp.config.country_rules
    if not rules.is_usable:
        return RemotePlan(work_mode=inp.work_mode)
    steps = []
    if rules.value.remote_first_day_required:
        steps = ["IT confirms the courier delivery status before the joining date.",
                 "HR confirms the remote accounts and access are active, using IT's confirmation.",
                 "HR sends the remote first-day instructions after approval."]
    return RemotePlan(work_mode=inp.work_mode, country_code=rules.value.country_code,
                      courier_extra_days=rules.value.courier_extra_days,
                      remote_first_day_required=rules.value.remote_first_day_required, timezone=rules.value.timezone,
                      calendar_source="COUNTRY" if rules.value.calendar() is not None else "CLIENT", first_day_steps=steps)
