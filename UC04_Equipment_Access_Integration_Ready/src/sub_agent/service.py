"""The single UC-04 business service. The direct entry point and the parent-orchestrated entry point both call
`Uc04Service.evaluate` / `submit_eligible`; no business decision is made anywhere else (BR-23).

Local simulation only: the IT service desk and the parent case registry are mock ports.
"""

import hashlib
from datetime import date
from typing import Any

from pydantic import ValidationError

from sub_agent.audit import AuditLog
from sub_agent.contracts import (Blocker, CaseStatusView, DuplicateLink, ItemResult, ItemStatusView, NextAction, Proposal, Reassessment,
                            RejectedApproval, Risk, SubmissionReport, Uc04Input, Uc04Request, Uc04Result)
from sub_agent.evidence import collect_ticket_facts
from sub_agent.guardrails import assert_clean, detect_injection
from sub_agent.items import ItemEval, evaluate_item
from sub_agent.lifecycle import POST_TICKET_STATES, InvalidTransition, path, transition
from sub_agent.policy import Policy, PolicyViolation, ToolGateway
from sub_agent.ports import CaseContextPort, ITServiceDeskPort, ParentCase
from sub_agent.readiness import build_next_actions, derive_status
from sub_agent.risk import assess_client_site, case_risks, item_risks, remote_plan
from sub_agent.transfer import plan_transfer
from sub_agent.rules import blocker, check_config, config_gap_blockers, start_gate
from sub_agent.states import (ApprovalState, CaseStatus, ItemState, Owner, ParentCaseStatus, Severity,
                         TicketStatus)
from sub_agent.store import CaseRecord, CaseRepository, CaseStore, IdempotencyConflict, ItemRecord


# Held in `adapter_errors` while an IT-desk call may be in flight; replaced by the error class if it fails, removed if it answers. A value that is
# still there after a crash means "outcome unknown": the request is not sent again until a person acknowledges (BR-18).
SUBMISSION_IN_DOUBT = "SUBMISSION_IN_DOUBT"
def config_digest(config) -> str:
    """A short content digest of the configuration used for a decision. A label can stay the same while content changes; this cannot."""
    return hashlib.sha256(config.model_dump_json().encode()).hexdigest()[:16]


NOTIFY_IT_WITHDRAWAL = "NOTIFY_IT_WITHDRAWAL"  # withdrawal: UC-04 only tells IT; IT owns every follow-on action
NOTIFY_IT_SUPERSEDED = "NOTIFY_IT_SUPERSEDED_REQUEST"


class Uc04ValidationError(ValueError):
    def __init__(self, fields: list[str]):
        super().__init__(f"invalid input fields: {', '.join(fields)}")
        self.fields = fields


class UnknownCaseError(LookupError):
    """UC-04 never creates an onboarding case (BR-24); the case must already exist in the parent workflow."""


class CaseMismatchError(ValueError):
    pass


class DuplicateCaseError(ValueError):
    def __init__(self, existing_case_id: str):
        super().__init__(f"an onboarding case already exists for this employee: {existing_case_id}")
        self.existing_case_id = existing_case_id


def _request_id(case_id: str, request_key: str) -> str:
    return f"{case_id}:{request_key}"


class Uc04Service:
    def __init__(self, *, cases: CaseContextPort, it_desk: ITServiceDeskPort, policy: Policy | None = None,
                 audit: AuditLog | None = None, store: CaseRepository | None = None):
        self.cases, self.it_desk = cases, it_desk
        self.policy = policy or Policy.load()
        self.audit = audit or AuditLog()
        self.store: CaseRepository = store if store is not None else CaseStore()
        self.gateway = ToolGateway(self.policy)

    # ------------------------------------------------------------------ input
    def parse_input(self, payload: Any) -> Uc04Input:
        if isinstance(payload, Uc04Input):
            return payload
        try:
            return Uc04Input.model_validate(payload)
        except ValidationError as e:
            fields = sorted({".".join(str(p) for p in err["loc"]) for err in e.errors()})
            self.audit.record("input.validation_failed", case_id=None, rule_id="BR-18",
                              reason=f"invalid fields: {', '.join(fields[:20])}")
            raise Uc04ValidationError(fields) from None

    # ------------------------------------------------------------------ evaluate
    def evaluate(self, request: Uc04Request) -> Uc04Result:
        caller, inp = request.caller, request.input
        a, cid = self.audit, request.input.case_id
        mark = a.mark()
        a.record("invocation.received", case_id=cid, rule_id="BR-23", reason=f"mode={caller.invocation_mode.value}")
        try:
            self.policy.check_caller(caller.roles)
        except PolicyViolation:
            a.record("caller.rejected", case_id=cid, rule_id="GR-02", reason="caller is not allowed to invoke UC-04")
            raise

        parent = self._resolve_case(inp)
        a.bind_context(client_id=parent.client_id)  # the verified parent's client, for a sink; never part of an entry
        payload_hash = hashlib.sha256(inp.model_dump_json().encode()).hexdigest()
        record = self.store.get(cid)
        if record and caller.idempotency_key in record.results:
            cached_hash, cached_version, cached_parent, cached = record.results[caller.idempotency_key]
            if cached_hash != payload_hash:
                a.record("idempotency.conflict", case_id=cid, rule_id="BR-21", reason="key reused with a different payload")
                raise IdempotencyConflict("idempotency key reused with a different payload")
            # A stored result is replayed only while nothing it depended on has changed: the case state AND the parent status
            # (a withdrawn or held parent must never be hidden behind an old answer; BR-22, EC-20).
            if cached_version == self.store.version_for(cid) and cached_parent == parent.status.value:
                a.record("replay.served", case_id=cid, rule_id="BR-21", reason="identical request, stored result returned")
                return cached.model_copy(deep=True)

        a.record("input.validated", case_id=cid, rule_id="BR-18")
        a.record("case.resolved", case_id=cid, rule_id="BR-24", reason="existing parent case reused; none created")
        self.store.begin(cid)
        record = self.store.get(cid)  # inside the unit of work: the aggregate that will be written back (same object in memory)
        snapshot = self.store.snapshot(cid)
        try:
            if parent.status in (ParentCaseStatus.WITHDRAWN, ParentCaseStatus.CANCELLED):
                result = self._cancelled(inp, record)
            else:
                result = self._assess(inp, parent)
            result.caller = {"invocation_mode": caller.invocation_mode.value, "run_id": caller.run_id,
                             "idempotency_key": caller.idempotency_key, "workflow_instance": caller.workflow_instance}
            a.record("readiness.evaluated", case_id=cid, rule_id="BR-17", new_status=result.overall_status.value,
                     reason=result.readiness_reason)
            result.audit_events = a.since(mark, cid)
            assert_clean(result.model_dump(mode="json"), "result")  # fail closed on any sensitive content (BR-15)
        except Exception:
            # Never leave half-applied state behind a failed call: a retry must behave as a first attempt (BR-18).
            self.store.restore(cid, snapshot)
            self.store.rollback(cid)
            a.record("state.rolled_back", case_id=cid, rule_id="BR-18", reason="evaluation failed; stored state restored")
            raise
        record = self.store.get(cid)
        if record:
            record.results[caller.idempotency_key] = (payload_hash, self.store.version_for(cid), parent.status.value,
                                                      result.model_copy(deep=True))
        self.store.commit(cid)
        return result

    def _resolve_case(self, inp: Uc04Input) -> ParentCase:
        parent = self.cases.get_case(inp.case_id)
        if parent is None:
            self.audit.record("case.unknown", case_id=inp.case_id, rule_id="BR-24",
                              reason="no such onboarding case; UC-04 does not create one")
            raise UnknownCaseError(inp.case_id)
        if parent.employee_ref != inp.employee_ref or parent.client_id != inp.client_id:
            self.audit.record("case.mismatch", case_id=inp.case_id, rule_id="BR-02",
                              reason="employee or client does not match the parent case")
            raise CaseMismatchError("employee or client does not match the parent case")
        existing = self.cases.case_for_employee(inp.employee_ref, client_id=inp.client_id)  # scoped: never another client's case
        if existing is not None and existing != inp.case_id:
            self.audit.record("case.duplicate", case_id=inp.case_id, rule_id="EC-38",
                              reason=f"existing case {existing} is linked instead")
            raise DuplicateCaseError(existing)
        return parent

    # ------------------------------------------------------------------ cancelled parent case
    def _cancelled(self, inp: Uc04Input, record: CaseRecord | None) -> Uc04Result:
        a, cid = self.audit, inp.case_id
        items: list[ItemResult] = []
        drafts: list[Proposal] = []
        for key, rec in (record.items.items() if record else []):
            prev = rec.state
            if prev != ItemState.CANCELLED:
                a.record("item.transition", case_id=cid, request_id=rec.request_id, rule_id="DL-15",
                         previous_status=prev.value, new_status=ItemState.CANCELLED.value, reason="parent case cancelled")
                rec.state = ItemState.CANCELLED
                rec.history.append((prev.value, ItemState.CANCELLED.value))
                self.store.touch()
                if rec.ticket_id or prev in POST_TICKET_STATES:
                    drafts.append(Proposal(proposal_type=NOTIFY_IT_WITHDRAWAL, request_id=rec.request_id,
                                           description="Draft notice to IT: the onboarding case was withdrawn. IT decides any follow-on action. "
                                                       "UC-04 cancels, unassigns, collects and revokes nothing.",
                                           requires_approval_by=Owner.HR_OFFICER))
            items.append(ItemResult(
                request_id=rec.request_id, request_key=key, item_kind=rec.item_kind, item_id=rec.item_id, in_matrix=None,
                exception_required=False, required_approver="NOT_EVALUATED", approval_state=ApprovalState.NOT_REQUIRED,
                exception_state=ApprovalState.NOT_REQUIRED, state=ItemState.CANCELLED, ticket_id=rec.ticket_id))
        a.record("cancellation.applied", case_id=cid, rule_id="DL-15", new_status=CaseStatus.CANCELLED.value,
                 reason="no further simulated processing")
        actions = [NextAction(action="Review and approve the notice to IT.", owner=Owner.HR_OFFICER)] if drafts else []
        return Uc04Result(case_id=cid, overall_status=CaseStatus.CANCELLED,
                          readiness_reason="The parent onboarding case is withdrawn or cancelled. No further processing.",
                          items=items, cancellation_drafts=drafts, next_actions=actions,
                          config_version=inp.config.version, config_digest=config_digest(inp.config))

    # ------------------------------------------------------------------ main assessment
    def _assess(self, inp: Uc04Input, parent: ParentCase) -> Uc04Result:
        a, cid = self.audit, inp.case_id
        gate = start_gate(inp, parent.status)
        a.record("start_gate.evaluated", case_id=cid, rule_id="BR-01", new_status="BLOCKED" if gate else "PASSED",
                 reason=", ".join(b.code for b in gate) or None)
        if gate:
            return Uc04Result(case_id=cid, overall_status=CaseStatus.BLOCKED,
                              readiness_reason=f"Blocked: {', '.join(sorted(b.code for b in gate))}. Nothing was prepared.",
                              blockers=gate, next_actions=[NextAction(action=b.next_action, owner=b.owner) for b in gate],
                              config_version=inp.config.version, config_digest=config_digest(inp.config))

        record = self.store.ensure(cid, inp.employee_ref, inp.client_id)
        gaps = check_config(inp)
        a.record("config.resolved", case_id=cid, rule_id="BR-02", reason=f"{len(gaps)} configuration gap(s)")
        for g in gaps:
            a.record("config.gap", case_id=cid, rule_id="BR-03", reason=g.code, next_action="Ask HR for the missing configuration.",
                     owner=Owner.HR_OFFICER)

        blockers: list[Blocker] = config_gap_blockers(gaps)
        risks: list[Risk] = []
        proposals: list[Proposal] = []
        extra_actions: list[NextAction] = []

        self._note_client_overrides(inp, extra_actions)
        reassessment = self._reassess(inp, record)
        site_drafts = self._cancel_old_client_site_requests(inp, record, reassessment)  # EC-35
        if {"config_version", "config_digest"} & set(reassessment.changed_fields):
            extra_actions.append(NextAction(action="The client configuration changed since the last evaluation. HR re-checks the approvals and due dates.",
                                            owner=Owner.HR_OFFICER))
        if site_drafts:
            extra_actions.append(NextAction(action="Review and approve the notice to IT about the old client-site requests.",
                                            owner=Owner.HR_OFFICER))

        # ---- requests: dedupe by ID, never by name (BR-14). A request key cancelled by a client change is never reused (EC-35).
        all_requested = inp.requested_access + inp.requested_equipment
        requested = [i for i in all_requested if i.request_key not in record.archived]
        refused = [i for i in all_requested if i.request_key in record.archived]
        known = {_request_id(cid, i.request_key): None for i in requested}
        blockers += [blocker("REQUEST_KEY_CANCELLED_BY_CLIENT_CHANGE", Severity.REVIEW,
                             f"Request {i.request_key} was cancelled by a client change and cannot be reused.", Owner.ACCOUNT_MANAGER,
                             "Raise the client-site request again under a new request key for the new client.", "EC-35",
                             _request_id(cid, i.request_key)) for i in refused]
        stored_tickets = {rec.request_id: rec.ticket_id for rec in record.items.values()}
        for rid in known:
            stored_tickets.setdefault(rid, None)
        facts, ignored_updates = collect_ticket_facts(inp, stored_tickets)

        kept, duplicates = self._dedupe(requested, cid, facts)
        for d in duplicates:
            a.record("duplicate_request_ignored", case_id=cid, request_id=d.linked_request_id, rule_id="BR-14",
                     reason=d.reason_code)
        for item in kept:
            hits = detect_injection(item.note)
            if hits:
                a.record("untrusted_text.flagged", case_id=cid, request_id=_request_id(cid, item.request_key), rule_id="GR-10",
                         reason=f"treated as data, not acted on: {', '.join(hits)}")

        cs = assess_client_site(inp, gaps)
        evals = [evaluate_item(inp, i, _request_id(cid, i.request_key), gaps, facts[_request_id(cid, i.request_key)],
                               cs.confirmed) for i in kept]

        # ---- state transitions (BR-07, TC-30)
        results: list[ItemResult] = []
        superseded = self._superseded(evals)
        rejected_evidence: list[RejectedApproval] = []
        for ev in evals:
            state, extra = self._apply_transitions(record, ev)
            ev.blockers += extra
            ev.target = state
            self._audit_approvals(cid, ev)
            if state == ItemState.ITEM_COMPLETED and ev.delivered_on is not None and ev.delivered_on > inp.joining_date:
                # EC-29: a finished item is not "at risk" any more, but the late delivery stays on the record.
                a.record("delivery.after_joining", case_id=cid, request_id=ev.request_id, rule_id="EC-29",
                         reason=f"delivered {ev.delivered_on.isoformat()}, after the joining date {inp.joining_date.isoformat()}")
            rejected_evidence += ev.outcome.rejected_evidence
            attempts = record.items[ev.item.request_key].submission_attempts if ev.item.request_key in record.items else 0
            results.append(self._item_result(ev, state, ev.request_id in superseded, attempts))
        results += self._archived_results(record)  # superseded: reported, never counted towards readiness
        unknown_rids = {x.request_id for x in inp.approval_records} - set(known)
        for approval in (x for x in inp.approval_records if x.request_id in unknown_rids):
            rejected_evidence.append(RejectedApproval(approval_id=approval.approval_id, request_id=approval.request_id,
                                                      reason_code="UNKNOWN_REQUEST"))
            a.record("approval.rejected", case_id=cid, request_id=approval.request_id, rule_id="BR-16",
                     reason="UNKNOWN_REQUEST")
        reassessment.reapproval_request_ids = sorted({r.request_id for r in rejected_evidence if r.reason_code == "STALE_SCOPE"})

        for ev in evals:
            if ev.request_id in superseded:
                continue
            blockers += ev.blockers
            ev_risks, ev_props = item_risks(inp, ev)
            risks += ev_risks
            proposals += ev_props
        for rid, code in ignored_updates:
            a.record("ticket_update.ignored", case_id=cid, request_id=rid, rule_id="BR-09", reason=code)
            blockers.append(blocker("TICKET_UPDATE_IGNORED", Severity.REVIEW, f"A ticket update was ignored: {code}.",
                                    Owner.IT_SERVICE_DESK, "Confirm the correct ticket status with IT. No blind retry.",
                                    "BR-09", rid if rid in known else None))
        for rid, error in record.adapter_errors.items():
            blockers.append(blocker("ADAPTER_ERROR", Severity.REVIEW,
                                    f"The last simulated submission failed ({error}). State was preserved.",
                                    Owner.IT_SERVICE_DESK, "Confirm with IT before any retry. Do not retry blindly.", "BR-18", rid))
        if not kept:
            blockers.append(blocker("NO_ITEMS_REQUESTED", Severity.REVIEW, "No equipment or access has been requested.",
                                    Owner.HR_OFFICER, "Ask HR which equipment and access this joiner needs.", "DL-17"))

        c_risks, c_props, c_blockers = case_risks(inp)
        t_plan = None
        if inp.transfer is not None:  # EC-42
            t_plan, t_props, t_blockers = plan_transfer(inp)
            c_props, c_blockers = c_props + t_props, c_blockers + t_blockers
            a.record("transfer.planned", case_id=cid, rule_id="EC-42",
                     reason=f"transfer {inp.transfer.transfer_ref}: {len(t_props)} proposal(s); no case or identity created",
                     owner=Owner.HR_OFFICER)
        blockers += cs.blockers + c_blockers
        risks += cs.risks + c_risks
        proposals += cs.proposals + c_props
        blockers = _unique_blockers(blockers)
        a.record("risk.calculated", case_id=cid, rule_id="DL-07", reason=f"{len(risks)} risk(s), {len(blockers)} blocker(s)")

        status, reason = derive_status(cancelled=False, blockers=blockers, risks=risks, items=results,
                                       clearance_required=inp.client_site_required, clearance_ok=cs.confirmed)
        approver_owner = {ev.request_id: (ev.std.owner if ev.target == ItemState.APPROVAL_PENDING
                                          else (ev.exc.owner if ev.exc else ev.std.owner)) for ev in evals}
        actions = build_next_actions(results, blockers, risks, approver_owner)
        for extra in extra_actions:
            if extra not in actions:
                actions.append(extra)
        for blk in blockers:
            a.record("blocker.raised", case_id=cid, request_id=blk.request_id, rule_id=blk.rule_id, reason=blk.code,
                     next_action=blk.next_action, owner=blk.owner)

        record.fingerprint = self._fingerprint(inp)
        return Uc04Result(
            case_id=cid, overall_status=status, readiness_reason=reason, items=results, blockers=blockers, risks=risks,
            proposals=_unique_proposals(proposals), cancellation_drafts=site_drafts, rejected_approvals=rejected_evidence,
            duplicates_ignored=duplicates,
            next_actions=actions, reassessment=reassessment, remote_plan=remote_plan(inp), transfer_plan=t_plan, config_version=inp.config.version,
            config_digest=config_digest(inp.config), config_gaps=[g.code for g in gaps], clearance_status=cs.status)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _fingerprint(inp: Uc04Input) -> dict[str, str]:
        return {"client_id": inp.client_id, "role_id": inp.role_id, "work_location": inp.work_location,
                "work_mode": inp.work_mode.value, "client_site_required": str(inp.client_site_required),
                "joining_date": inp.joining_date.isoformat(), "form_version": inp.form_version,
                "config_version": inp.config.version, "config_digest": config_digest(inp.config),
                "project_id": inp.project_id or "", "transfer_ref": inp.transfer.transfer_ref if inp.transfer else ""}

    def _reassess(self, inp: Uc04Input, record: CaseRecord) -> Reassessment:
        previous = record.fingerprint
        now = self._fingerprint(inp)
        changed = sorted(k for k in now if previous and previous.get(k, now[k]) != now[k])
        if not changed:
            return Reassessment(required=False)
        self.audit.record("reassessment.triggered", case_id=inp.case_id, rule_id="EC-11",
                          reason=f"changed: {', '.join(changed)}", next_action="Re-plan due dates and check approvals.",
                          owner=Owner.HR_OFFICER)
        return Reassessment(required=True, changed_fields=changed,
                            previous_joining_date=(date.fromisoformat(previous["joining_date"])
                                                   if "joining_date" in changed else None),
                            previous_client_id=previous["client_id"] if "client_id" in changed else None,
                            previous_project_id=(previous.get("project_id") or None) if "project_id" in changed else None,
                            previous_config_version=previous["config_version"] if "config_version" in changed else None,
                            previous_config_digest=previous["config_digest"] if "config_digest" in changed else None)

    def _cancel_old_client_site_requests(self, inp: Uc04Input, record: CaseRecord, reassessment: Reassessment) -> list[Proposal]:
        """EC-35: "Project or client changes before joining -> new onboarding form version; cancel the old client-site requests and
        start the new ones". Runs only after the start gate, so the new form version is already approved (DL-02). The old requests are
        cancelled locally and archived; anything already raised gets a DRAFT notice to IT for HR to approve (IT owns every follow-on action). Nothing is sent, released
        or ordered. Non-client-site items are not touched: their approvals are scope-checked as usual."""
        project_changed = "project_id" in reassessment.changed_fields and bool(record.fingerprint.get("project_id"))  # a first assignment is not a change
        if "client_id" not in reassessment.changed_fields and not project_changed:
            return []
        a, cid = self.audit, inp.case_id
        drafts: list[Proposal] = []
        moved = 0
        for key, rec in list(record.items.items()):
            if not rec.client_site:
                continue
            prev = rec.state
            if prev != ItemState.CANCELLED:
                transition(prev, ItemState.CANCELLED)
                a.record("item.transition", case_id=cid, request_id=rec.request_id, rule_id="EC-35", previous_status=prev.value,
                         new_status=ItemState.CANCELLED.value, reason="client changed; old client-site request cancelled")
                rec.state = ItemState.CANCELLED
                rec.history.append((prev.value, ItemState.CANCELLED.value))
                if rec.ticket_id or prev in POST_TICKET_STATES:
                    drafts.append(Proposal(proposal_type=NOTIFY_IT_SUPERSEDED, request_id=rec.request_id,
                                           description="Draft notice to IT: this client-site request was superseded by a client change. IT decides "
                                                       "any follow-on action. UC-04 cancels, unassigns, collects and revokes nothing.",
                                           requires_approval_by=Owner.HR_OFFICER))
            record.archived[key] = record.items.pop(key)
            moved += 1
        if moved:
            a.record("client_change.client_site_items_cancelled", case_id=cid, rule_id="EC-35",
                     reason=f"{moved} old client-site request(s) cancelled",
                     next_action="Review and approve the notice to IT." if drafts else None,
                     owner=Owner.HR_OFFICER if drafts else None)
            self.store.touch()
        return drafts

    @staticmethod
    def _archived_results(record: CaseRecord) -> list[ItemResult]:
        return [ItemResult(request_id=rec.request_id, request_key=key, item_kind=rec.item_kind, item_id=rec.item_id, in_matrix=None,
                           exception_required=False, required_approver="NOT_EVALUATED", approval_state=ApprovalState.NOT_REQUIRED,
                           exception_state=ApprovalState.NOT_REQUIRED, state=ItemState.CANCELLED, ticket_id=rec.ticket_id,
                           superseded=True) for key, rec in record.archived.items()]

    def _note_client_overrides(self, inp: Uc04Input, actions: list[NextAction]) -> None:
        o = inp.config.client_overrides
        if o.skip_standard_approval or o.allow_out_of_matrix_without_exception:
            self.audit.record("client_instruction_conflict", case_id=inp.case_id, rule_id="GR-17",
                              reason="a client instruction conflicts with a mandatory guardrail and was ignored",
                              next_action="Notify the client HR admin.", owner=Owner.CLIENT_HR_ADMIN)
            actions.append(NextAction(action="Notify the client HR admin that a client instruction conflicts with a "
                                             "mandatory guardrail and was ignored.", owner=Owner.CLIENT_HR_ADMIN))

    @staticmethod
    def _dedupe(requested, cid: str, facts) -> tuple[list, list[DuplicateLink]]:
        kept, links = [], []
        by_key: dict[str, str] = {}
        by_content: dict[tuple[str, str], str] = {}
        for item in requested:
            rid = _request_id(cid, item.request_key)
            content = (item.item_kind.value, item.item_id)
            if item.request_key in by_key:
                links.append(DuplicateLink(request_key=item.request_key, linked_request_id=by_key[item.request_key],
                                           reason_code="DUPLICATE_REQUEST_KEY"))
            elif content in by_content and facts[by_content[content]].latest != TicketStatus.REJECTED:
                links.append(DuplicateLink(request_key=item.request_key, linked_request_id=by_content[content],
                                           reason_code="DUPLICATE_ITEM_FOR_CASE"))
            else:
                kept.append(item)
                by_key[item.request_key] = rid
                by_content[content] = rid
        return kept, links

    @staticmethod
    def _superseded(evals: list[ItemEval]) -> set[str]:
        out = set()
        for ev in evals:
            if ev.target == ItemState.REJECTED_CORRECTION_REQUIRED:
                content = (ev.item.item_kind, ev.item.item_id)
                if any(o is not ev and (o.item.item_kind, o.item.item_id) == content
                       and o.target != ItemState.REJECTED_CORRECTION_REQUIRED for o in evals):
                    out.add(ev.request_id)
        return out

    def _apply_transitions(self, record: CaseRecord, ev: ItemEval) -> tuple[ItemState, list[Blocker]]:
        a, cid, item = self.audit, record.case_id, ev.item
        existing = record.items.get(item.request_key)
        prev = existing.state if existing else None
        try:
            steps = path(prev, ev.target, kind=item.item_kind, exception_item=ev.exception_required,
                         collection_required=ev.collection_required, clearance_required=ev.clearance_required)
        except InvalidTransition:
            a.record("transition.rejected", case_id=cid, request_id=ev.request_id, rule_id="BR-07",
                     previous_status=prev.value if prev else None, new_status=ev.target.value,
                     reason="illegal state transition; stored state preserved", owner=Owner.HR_OFFICER)
            return (prev or ItemState.BLOCKED), [blocker("INVALID_STATE_TRANSITION", Severity.REVIEW,
                                  "The evidence implies an illegal state change; the stored state was kept.",
                                  Owner.HR_OFFICER, "Review the evidence. Completion evidence cannot be withdrawn silently.",
                                  "BR-07", ev.request_id)]
        if existing is None:
            existing = record.items[item.request_key] = ItemRecord(ev.request_id, item.request_key, item.item_kind,
                                                                   item.item_id, client_site=item.client_site)
            self.store.touch()
        if ev.ticket and ev.ticket.ticket_id and existing.ticket_id != ev.ticket.ticket_id:
            existing.ticket_id = ev.ticket.ticket_id
            self.store.touch()
        if existing.ticket_id:
            record.adapter_errors.pop(ev.request_id, None)
        last = prev
        for step in steps:
            a.record("item.transition", case_id=cid, request_id=ev.request_id, rule_id="BR-07",
                     previous_status=last.value if last else None, new_status=step.value)
            existing.history.append((last.value if last else None, step.value))
            last = step
        if steps and existing.state != steps[-1]:
            existing.state = steps[-1]
            self.store.touch()
        return existing.state, []

    def _audit_approvals(self, cid: str, ev: ItemEval) -> None:
        a, rid = self.audit, ev.request_id
        for r in ev.outcome.rejected_evidence:
            a.record("approval.rejected", case_id=cid, request_id=rid, rule_id="BR-16", reason=r.reason_code)
        if ev.outcome.standard == ApprovalState.RECEIVED:
            a.record("approval.accepted", case_id=cid, request_id=rid, rule_id="BR-16", reason="standard approval valid")
        if ev.exc is not None and ev.outcome.exception == ApprovalState.RECEIVED:
            a.record("exception.accepted", case_id=cid, request_id=rid, rule_id="BR-05", reason="exception approval valid")
        if ev.exception_required:
            a.record("exception.required", case_id=cid, request_id=rid, rule_id="GR-03",
                     reason="item is not in the approved role matrix")
        for ref in ev.evidence_refs:
            a.record("evidence.accepted", case_id=cid, request_id=rid, rule_id="BR-17", reason=f"evidence {ref}")

    @staticmethod
    def _item_result(ev: ItemEval, state: ItemState, superseded: bool, submission_attempts: int = 0) -> ItemResult:
        approver = ev.std.display if not ev.exception_required else f"{ev.std.display} + {ev.exc.display}"
        return ItemResult(
            request_id=ev.request_id, request_key=ev.item.request_key, item_kind=ev.item.item_kind, item_id=ev.item.item_id,
            in_matrix=ev.in_matrix, exception_required=ev.exception_required,
            exception_reason="ITEM_NOT_IN_ROLE_MATRIX" if ev.exception_required else None, required_approver=approver,
            approval_state=ev.outcome.standard, exception_state=ev.outcome.exception, state=state,
            ticket_id=ev.ticket.ticket_id if ev.ticket else None, ticket_status=ev.ticket.latest if ev.ticket else None,
            provisioning_state=ev.provisioning_state, delivery_state=ev.delivery_state,
            collection_state=ev.collection_state, due_date=ev.due_date, lead_time_days=ev.lead_days,
            lead_time_source=ev.lead_source, evidence_refs=ev.evidence_refs, completed=state == ItemState.ITEM_COMPLETED,
            superseded=superseded, submission_attempts=submission_attempts)

    # ------------------------------------------------------------------ simulated submission (mock only)
    def submit_eligible(self, request: Uc04Request) -> SubmissionReport:
        result = self.evaluate(request)
        cid = request.input.case_id
        submitted: list[str] = []
        failed: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        record = self.store.get(cid)
        if result.overall_status == CaseStatus.CANCELLED or record is None:
            skipped.append({"request_id": "*", "reason": "CASE_NOT_ACTIVE"})
            return SubmissionReport(result=result, skipped=skipped)
        changed = False
        for item in result.items:
            if item.state != ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION or item.superseded:
                continue
            rec = record.items[item.request_key]
            if item.request_id in record.adapter_errors:
                skipped.append({"request_id": item.request_id, "reason": "PREVIOUS_ADAPTER_ERROR_UNRESOLVED"})
                continue
            # 1. Record the INTENT and make it durable before anything leaves the system. "A call may be in flight" is held in the same
            #    place as a failed call, so if this process dies, or its answer is lost, the item stays unresolved and is never sent
            #    again without a person's acknowledgement. Success is never assumed. (No-ops in memory.)
            self.store.begin(cid)
            record = self.store.get(cid) or record
            rec = record.items[item.request_key]
            rec.submission_attempts += 1
            record.adapter_errors[item.request_id] = SUBMISSION_IN_DOUBT
            self.store.commit(cid)  # if this fails, the exception propagates and the desk is not called
            try:
                ticket = self.gateway.call("it_service_desk.submit_request", self.it_desk.submit_request, approved=True,
                                           request_id=item.request_id, case_id=cid, item_kind=item.item_kind.value,
                                           item_id=item.item_id)
            except Exception as e:  # adapter boundary: any failure must fail closed
                # 2a. Outcome: the call failed. Replace the in-doubt mark with the error class (never the message).
                self.store.begin(cid)
                record = self.store.get(cid) or record
                record.adapter_errors[item.request_id] = type(e).__name__
                self.audit.record("adapter.failed", case_id=cid, request_id=item.request_id, rule_id="BR-18",
                                  reason=f"simulated submission failed ({type(e).__name__}); state preserved",
                                  next_action="Confirm with IT before any retry.", owner=Owner.IT_SERVICE_DESK)
                failed.append({"request_id": item.request_id, "error": type(e).__name__})
                self.store.commit(cid)
                changed = True
                continue
            # 2b. Outcome: the desk answered. Record the ticket and clear the in-doubt mark in one commit.
            self.store.begin(cid)
            record = self.store.get(cid) or record
            record.adapter_errors.pop(item.request_id, None)
            record.items[item.request_key].ticket_id = ticket.ticket_id
            self.audit.record("ticket.created", case_id=cid, request_id=item.request_id, rule_id="BR-08",
                              reason="mock ticket created; nothing real was submitted")
            submitted.append(item.request_id)
            self.store.commit(cid)
            changed = True
        if changed:
            self.store.touch()
            result = self.evaluate(request)
        return SubmissionReport(result=result, submitted=submitted, failed=failed, skipped=skipped)

    def acknowledge_adapter_error(self, case_id: str, request_id: str, roles: list[str]) -> bool:
        """A person confirms with IT that a retry is safe. Until then the request is not resubmitted. True when an error was cleared."""
        self.policy.check_acknowledger(roles)
        self.store.begin(case_id)
        try:
            record = self.store.get(case_id)
            with self.audit.context(client_id=record.client_id if record else None):
                if record and record.adapter_errors.pop(request_id, None) is not None:
                    self.audit.record("adapter_error.acknowledged", case_id=case_id, request_id=request_id, rule_id="BR-18",
                                      reason="a person confirmed a retry is safe")
                    self.store.touch()
                    self.store.commit(case_id)
                    return True
            self.store.rollback(case_id)  # nothing to acknowledge: end the unit of work without a change
            return False
        except Exception:
            self.store.rollback(case_id)
            raise


    def case_status(self, case_id: str, client_id: str, roles: list[str]) -> CaseStatusView:
        """Read-only: what UC-04 has stored for a case, for reconciliation after a crash or an uncertain call. Nothing is evaluated or changed.
        Refused for an unknown case and for a case of another client alike (the answer never reveals another tenant's case)."""
        self.policy.check_caller(roles)
        with self.audit.context(client_id=client_id):
            parent = self.cases.get_case(case_id)
            if parent is None or parent.client_id != client_id:
                self.audit.record("status.refused", case_id=None, rule_id="GR-16", reason="no such case for this client")
                raise UnknownCaseError(case_id)
            self.audit.record("status.read", case_id=case_id, rule_id="GR-09", reason="stored state read; nothing changed")
            record = self.store.get(case_id)
            items = []
            for archived, source in ((False, record.items if record else {}), (True, record.archived if record else {})):
                for key, rec in source.items():
                    items.append(ItemStatusView(request_id=rec.request_id, request_key=key, item_kind=rec.item_kind, item_id=rec.item_id,
                                                state=rec.state, ticket_id=rec.ticket_id, submission_attempts=rec.submission_attempts,
                                                adapter_error=record.adapter_errors.get(rec.request_id) if record else None, archived=archived))
            return CaseStatusView(case_id=case_id, client_id=client_id, parent_status=parent.status.value, items=items)


def _unique_blockers(blockers: list[Blocker]) -> list[Blocker]:
    seen, out = set(), []
    for b in blockers:
        key = (b.code, b.request_id, b.reason)
        if key not in seen:
            seen.add(key)
            out.append(b)
    return out


def _unique_proposals(proposals: list[Proposal]) -> list[Proposal]:
    seen, out = set(), []
    for p in proposals:
        key = (p.proposal_type, p.request_id)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out
