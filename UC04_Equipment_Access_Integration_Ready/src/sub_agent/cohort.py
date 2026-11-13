"""EC-40: a cohort of people with the same joining date. One case each, one cohort view, shared collection slots.

Each person keeps their own case, request ids, approvals and audit trail; nothing is merged into one case. This module only reads the
individual results to build a cohort view, and plans collection slots from the capacity IT reports. A slot is a RECOMMENDATION until IT confirms
it; UC-04 records a booking only from an IT confirmation. UC-05 owns orientation; UC-04 only supplies equipment and collection readiness.
"""

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from sub_agent.contracts import Uc04Result
from sub_agent.ports import SlotOffer
from sub_agent.states import CaseStatus


class CohortRow(BaseModel):
    case_id: str
    overall_status: str
    ready: bool
    pending_approvals: int
    open_it_requests: int
    risks: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    collection_states: dict[str, str] = Field(default_factory=dict)  # request id -> collection state


class CohortView(BaseModel):
    cohort_ref: str
    size: int
    by_status: dict[str, int]
    ready: int
    all_ready: bool
    delayed_or_blocked: list[str]
    failed_cases: dict[str, str] = Field(default_factory=dict)  # case id -> error class, for a case that could not be evaluated
    rows: list[CohortRow]


def cohort_view(cohort_ref: str, results: dict[str, Uc04Result], failed: dict[str, str] | None = None) -> CohortView:
    rows: list[CohortRow] = []
    for case_id in sorted(results):
        r = results[case_id]
        rows.append(CohortRow(
            case_id=case_id, overall_status=r.overall_status.value, ready=r.overall_status == CaseStatus.READY,
            pending_approvals=sum(1 for i in r.items if i.approval_state.value in ("PENDING", "REQUESTED") or i.exception_state.value == "PENDING"),
            open_it_requests=sum(1 for i in r.items if i.ticket_id and not i.completed),
            risks=sorted({x.code for x in r.risks}), blockers=sorted({b.code for b in r.blockers}),
            collection_states={i.request_id: i.collection_state for i in r.items if i.collection_state and i.collection_state != "NOT_REQUIRED"}))
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.overall_status] = by_status.get(row.overall_status, 0) + 1
    failed = dict(failed or {})
    ready = sum(1 for row in rows if row.ready)
    return CohortView(cohort_ref=cohort_ref, size=len(rows) + len(failed), by_status=by_status, ready=ready,
                      all_ready=bool(rows) and not failed and ready == len(rows),
                      delayed_or_blocked=sorted([r.case_id for r in rows if r.overall_status in ("BLOCKED", "AT_RISK", "NEEDS_HUMAN_REVIEW")] + list(failed)),
                      failed_cases=failed, rows=rows)


class SlotRecommendation(BaseModel):
    case_id: str
    request_id: str
    slot_id: str | None
    slot_date: str | None
    status: str  # RECOMMENDED (awaiting IT), CONFIRMED (IT said so), or UNASSIGNED
    reason: str | None = None


@dataclass
class SlotPlan:
    cohort_ref: str
    capacity: dict[str, int]
    dates: dict[str, str]
    recommendations: dict[str, SlotRecommendation] = field(default_factory=dict)  # by request id: one booking per request, never two

    def used(self, slot_id: str) -> int:
        return sum(1 for r in self.recommendations.values() if r.slot_id == slot_id and r.status in ("RECOMMENDED", "CONFIRMED"))

    def confirm(self, request_id: str, slot_id: str) -> SlotRecommendation:
        """Record a booking ONLY because IT confirmed it. Capacity and one-booking-per-request still hold."""
        rec = self.recommendations.get(request_id)
        if rec is None or slot_id not in self.capacity:
            raise KeyError("unknown request or slot")
        if rec.slot_id != slot_id and self.used(slot_id) >= self.capacity[slot_id]:
            raise ValueError("slot capacity would be exceeded")
        updated = rec.model_copy(update={"slot_id": slot_id, "slot_date": self.dates[slot_id], "status": "CONFIRMED", "reason": None})
        self.recommendations[request_id] = updated
        return updated


def plan_slots(cohort_ref: str, requests: list[tuple[str, str]], offers: list[SlotOffer], confirmed: dict[str, str] | None = None) -> SlotPlan:
    """Recommend slots for (case_id, request_id) pairs within the capacity IT reported. A request appears once (no duplicate booking); one
    already confirmed by IT keeps its slot and uses its capacity. Anything that does not fit is UNASSIGNED, never over-booked."""
    ordered = sorted(offers, key=lambda s: (s.slot_date, s.slot_id))
    plan = SlotPlan(cohort_ref=cohort_ref, capacity={s.slot_id: max(s.capacity, 0) for s in ordered}, dates={s.slot_id: s.slot_date for s in ordered})
    seen: set[str] = set()
    confirmed = confirmed or {}
    pairs = []
    for case_id, request_id in requests:
        if request_id in seen:
            continue  # a duplicate request is never booked twice
        seen.add(request_id)
        pairs.append((case_id, request_id))
    for case_id, request_id in pairs:  # confirmed bookings first: they already hold capacity
        slot_id = confirmed.get(request_id)
        if slot_id in plan.capacity and plan.used(slot_id) < plan.capacity[slot_id]:
            plan.recommendations[request_id] = SlotRecommendation(case_id=case_id, request_id=request_id, slot_id=slot_id,
                                                                  slot_date=plan.dates[slot_id], status="CONFIRMED")
    for case_id, request_id in pairs:
        if request_id in plan.recommendations:
            continue
        slot = next((s for s in ordered if plan.used(s.slot_id) < plan.capacity[s.slot_id]), None)
        if slot is None:
            plan.recommendations[request_id] = SlotRecommendation(case_id=case_id, request_id=request_id, slot_id=None, slot_date=None,
                                                                  status="UNASSIGNED", reason="CAPACITY_EXHAUSTED")
        else:
            plan.recommendations[request_id] = SlotRecommendation(case_id=case_id, request_id=request_id, slot_id=slot.slot_id,
                                                                  slot_date=slot.slot_date, status="RECOMMENDED")
    return plan
