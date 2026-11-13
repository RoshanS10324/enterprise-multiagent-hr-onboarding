"""End-to-end trace of UC-01 against the mapped business requirements.

Walks a real case from acceptance email to written record using the
container's own wiring, and checks each of R1-R6 from
specs/uc01-requirements.yaml against actual behaviour rather than
against the docs.

    uv run python scripts/verify_requirements.py            # concise
    uv run python scripts/verify_requirements.py --verbose  # full trace

The concise run prints one PASS/FAIL line per check. The verbose run adds
a labelled, step-by-step trace around the same calls -- INPUT, PROCESSING,
BUSINESS RULE, APPROVAL REQUIRED, APPROVAL DECISION, STATE CHANGE,
EXTERNAL OP, OUTPUT, AUDIT EVENT, ASSERTION RESULT -- built from what the
services actually returned and from state read back out of the repos.
Nothing in the trace is a canned success message.

Exits non-zero if any check fails, so it can gate a release the way the
eval runner does. Synthetic data only, and every salary value is masked
on the way to the terminal (see sub_agent.trace.Redactor).
"""

import argparse
import sys
from datetime import date, datetime, timezone
from typing import Any, Optional

from pydantic import SecretStr, ValidationError

from sub_agent.api.container import build_container
from sub_agent.api.settings import Settings
from sub_agent.compare_sources import DiscrepancyDecision
from sub_agent.hris_mapping import HRIS_FIELD_MAPPING, build_sensitive_payload
from sub_agent.hris_prepare import HRIS_SYSTEMS
from sub_agent.intake.email_intake import (
    IntakeOutcome,
    handle_acceptance_email,
    handle_form_approved,
    parse_acceptance_email,
)
from sub_agent.models import (
    CasePlacement,
    ApprovalStatus,
    Checkpoint,
    FormChangeRequest,
    FormStatus,
    OfferRecord,
    OnboardingForm,
    PersonRecord,
    PersonRecordKind,
    Role,
    SourceSnapshot,
    VariantFlags,
    Viewer,
    WorkLocationType,
)
from sub_agent.onboarding_entry import OpenCaseRequest, open_onboarding_case
from sub_agent.trace import (
    NOT_CAPTURED,
    AuditTap,
    Redactor,
    RunLedger,
    Tracer,
    check_approver_against_policy,
    derived,
    external_mode,
    render_summary,
)

CLIENT = "client-e2e"
OFFICER_ID = "hr-officer-1"
MANAGER_ID = "hr-manager-1"
SALARY = "Base: 118000 SYNTHETIC/yr; benefits: standard"
OTHER_SALARY = "Base: 999999 OTHER/yr"

RESULTS: dict[str, list[tuple[bool, str, str]]] = {}

REDACTOR = Redactor()
LEDGER = RunLedger()
TRACER = Tracer(enabled=False, redactor=REDACTOR, ledger=LEDGER)

# user_id -> role, for every actor this run creates. Used to check an
# approval's intended approver against the roles the policy file permits.
# An actor missing from here is reported as unknown, never guessed at.
ACTOR_ROLES: dict[str, str] = {}

# Stated at the end of every run. These are paths the verification does
# NOT establish, listed so a green run cannot be read as covering them.
UNVERIFIED_PATHS = [
    "No live or mocked Zoho integration exists. Every HRIS call in this run goes through "
    "StubHrisGateway, an in-memory dict (src/sub_agent/adapters/memory.py). build_container "
    "refuses to start outside APP_ENV=dev for exactly this reason.",
    "Rejection of an HC_01_FORM form change is implemented (form_changes.reject_form_change) "
    "but is not exercised by this run.",
    "Rejection of an HC_12_VARIANT decision is implemented (variants.decide_variant_approval, "
    "which puts the case ON_HOLD) but is not exercised by this run.",
    "DiscrepancyDecision.ACCEPT_AS_CHANGE is implemented but only records the decision; the "
    "resulting edit goes through form versioning and is not traced here.",
    "Approval expiry/escalation (ApprovalStatus.EXPIRED, ESCALATED) has no implementation, so "
    "'approval remains pending indefinitely' has no behaviour to show.",
    "Approver identity is a dev header-auth placeholder (docs/known-gaps.md); there is no "
    "directory behind the user ids shown above.",
]


def _with_placement(container):
    """Supply the dev simulator's placement, as a caller would.

    location_name is a required caller-supplied value (approved
    2026-11-04) and nothing in an offer carries it, so IntakeDeps leaves
    it None and try_start parks the offer instead of inventing one. This
    verification script IS the caller, so it provides the value here
    rather than the container defaulting it for production.
    """
    container.intake_deps.placement = CasePlacement(
        location_name="Synthetic Office, Dubai"
    )
    return container


def register_actor(viewer: Viewer) -> Viewer:
    """Record an actor's role so approval-vs-policy checks can resolve it."""
    ACTOR_ROLES[viewer.user_id] = viewer.role.value
    return viewer

# The requirement text R1-R6 are checked against, kept in sync with
# specs/uc01-requirements.yaml (the owner's wording).
REQUIREMENT_TEXT = {
    "R1": "R1: Start onboarding only after offer acceptance and approval of the internal onboarding form.",
    "R2": "R2: Create the onboarding case using the approved employee details.",
    "R3": "R3: Maintain the source details for designation, joining date, work location and approved offer terms.",
    "R4": "R4: Prepare employee records for Zoho People and Zoho Payroll.",
    "R5": "R5: Handle duplicate requests, rehires, internal transfers and changes to employee details.",
    "R6": "R6: Flag discrepancies between the onboarding form, offer and other records for HR approval.",
    "OUT": "Expected output: one employee onboarding case with a reliable source record and traceable status.",
}


def check(
    req: str,
    label: str,
    ok: bool,
    detail: str = "",
    expected: Any = None,
    actual: Any = None,
    reason: str = "",
) -> None:
    """Record one assertion.

    Concise mode prints the single PASS/FAIL line it always has. Verbose
    mode prints an ASSERTION RESULT section carrying the expected and
    actual values the verdict came from, so a reader can see WHY it
    passed rather than being told that it did.
    """
    RESULTS.setdefault(req, []).append((ok, label, detail))
    LEDGER.record_assertion(ok, req, label)
    if TRACER.enabled:
        TRACER.assertion(
            ok,
            req,
            label,
            expected=expected if expected is not None else True,
            actual=actual if actual is not None else ok,
            reason=reason or detail,
        )
    else:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {req}  {label}" + (f"  -- {detail}" if detail else ""))


def heading(concise_title: str) -> None:
    """The concise run's section banner. Verbose runs use scenario()."""
    if not TRACER.enabled:
        print(f"\n--- {concise_title} ---")


def scenario(
    scenario_id: str,
    requirement: str,
    purpose: str,
    expected: str,
    concise_title: str,
) -> None:
    TRACER.scenario(scenario_id, REQUIREMENT_TEXT.get(requirement, requirement), purpose, expected)
    heading(concise_title)


# --- state snapshots -------------------------------------------------------


def case_snapshot(deps, case_id: Optional[str]) -> dict[str, Any]:
    """A flat, redacted picture of everything UC-01 persists for a case.

    Read straight back out of the repositories, so a before/after pair
    shows what the operation really did to stored state.
    """
    if case_id is None:
        return {}
    case = deps.case_repo.get(case_id)
    if case is None:
        return {"case": "<not found>"}
    in_force = deps.form_repo.latest_approved(case_id)
    return {
        "case.case_id": case.case_id,
        "case.employee_no": case.employee_no,
        "case.status": case.status.value,
        "case.current_form_version": case.current_form_version,
        "case.candidate_name": case.candidate_name,
        "case.joining_date": case.joining_date.isoformat(),
        "case.work_location_type": case.work_location_type.value,
        "case.variants": {k: v for k, v in case.variants.model_dump().items() if v} or "<none set>",
        "case.reactivates_external_ref": case.reactivates_external_ref,
        "form.version_in_force": in_force.version if in_force else None,
        "form.versions": [
            {"version": f.version, "status": f.status.value, "form_id": f.form_id}
            for f in sorted(deps.form_repo.list_versions(case_id), key=lambda f: f.version)
        ],
        "approvals": [
            {
                "approval_id": a.approval_id,
                "checkpoint": a.checkpoint.value,
                "subject_ref": a.subject_ref,
                "status": a.status.value,
                "requested_to": a.requested_to,
                "decided_by": a.decided_by,
            }
            for a in deps.approval_repo.list_for_case(case_id)
        ],
        "discrepancies": [
            {
                "discrepancy_id": d.discrepancy_id,
                "field": d.field,
                "other_source": d.other_source,
                "status": d.status.value,
                "form_version": d.form_version,
            }
            for d in deps.discrepancy_repo.list_for_case(case_id)
        ],
        "hris_records": [
            {
                "record_id": r.record_id,
                "system": r.system.value,
                "form_version": r.form_version,
                "status": r.status.value,
                "external_ref": r.external_ref,
                "updates_external_ref": r.updates_external_ref,
                "last_error": r.last_error,
            }
            for r in sorted(
                deps.hris_record_repo.list_for_case(case_id),
                key=lambda r: (r.form_version, r.system.value),
            )
        ],
    }


def form_fields_view(form: OnboardingForm) -> dict[str, Any]:
    """The form as the report shows it. salary_and_terms is a sensitive
    field name, so the Redactor masks its value automatically.
    """
    return {
        "form_id": form.form_id,
        "case_id": form.case_id,
        "client_id": form.client_id,
        "version": form.version,
        "status": form.status.value,
        "candidate_name": form.candidate_name,
        "candidate_email": form.candidate_email,
        "designation": form.designation,
        "joining_date": form.joining_date.isoformat(),
        "work_location_type": form.work_location_type.value,
        "client_site_id": form.client_site_id,
        "salary_and_terms": form.salary_and_terms,
        "approved_by": form.approved_by,
        "approved_at": form.approved_at,
        "change_reason": form.change_reason,
    }


def validate_form_fields(fields: dict[str, Any]) -> tuple[Optional[OnboardingForm], dict[str, Any]]:
    """Run the real OnboardingForm validation and report it field by field.

    Returns (form or None, per-field report). The report is derived from
    pydantic's own ValidationError, not from a second copy of the rules.
    """
    try:
        form = OnboardingForm(**fields)
    except ValidationError as exc:
        errors: dict[str, Any] = {}
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "<model>"
            errors.setdefault(loc, []).append({"type": err["type"], "msg": err["msg"]})
        report = {}
        for name in sorted(set(fields) | set(errors)):
            if name in errors:
                report[name] = {"valid": False, "errors": errors[name]}
            else:
                report[name] = {"valid": True, "supplied": fields.get(name)}
        for name, errs in errors.items():
            if name not in report:
                report[name] = {"valid": False, "errors": errs}
        return None, report

    report = {}
    for name in sorted(fields):
        report[name] = {"valid": True, "value_after_validation": getattr(form, name, None)}
    return form, report


def approver_roles(container, checkpoint: Checkpoint) -> list[str]:
    """Who may decide this checkpoint, read from the loaded policy file."""
    rule = container.policy.decisions[checkpoint]
    return [r.value for r in rule.roles]


def separation_of_duties(container, checkpoint: Checkpoint) -> bool:
    return container.policy.decisions[checkpoint].separation_of_duties


def pending_approval(deps, case_id: str, checkpoint: Checkpoint, subject_ref: Optional[str] = None):
    for a in deps.approval_repo.list_for_case(case_id):
        if a.checkpoint is not checkpoint or a.status is not ApprovalStatus.PENDING:
            continue
        if subject_ref is not None and a.subject_ref != subject_ref:
            continue
        return a
    return None


class ApprovalGate:
    """Traces one approval across its whole life: raised -> decided.

    Holds the status read off the approval BEFORE the decision call, so
    the transition printed afterwards is observed rather than assumed,
    and re-reads the approval from the repository after the decision
    rather than trusting the return value.
    """

    def __init__(
        self,
        container,
        approval,
        why: str,
        blocks: str,
        awaiting: Any = None,
        blocked_if_rejected: Optional[str] = None,
    ) -> None:
        self.container = container
        self.approval_id = approval.approval_id
        self.checkpoint = approval.checkpoint
        self.status_before = approval.status
        self.blocked_if_rejected = blocked_if_rejected
        allowed = approver_roles(container, approval.checkpoint)
        TRACER.approval_required(
            approval,
            why=why,
            blocks=blocks,
            approver_roles=allowed,
            separation_of_duties=separation_of_duties(container, approval.checkpoint),
            awaiting=awaiting,
        )
        # Compared, not assumed: if the implementation addresses an
        # approval to an actor the policy does not let decide it, that is
        # reported here and carried into the run summary.
        TRACER.policy_check(check_approver_against_policy(approval, allowed, ACTOR_ROLES))

    def decided(
        self, effect: str, next_step: str, resulting_state: Optional[dict[str, Any]] = None
    ):
        approval = self.container.deps.approval_repo.get(self.approval_id)
        TRACER.approval_decision(
            approval,
            effect=effect,
            next_step=next_step,
            status_before=self.status_before,
            blocked_if_rejected=self.blocked_if_rejected,
            resulting_state=resulting_state,
        )
        return approval


def employee_count(gateway) -> Any:
    """Distinct employees in the target system, when the gateway reports it."""
    counter = getattr(gateway, "written_count", None)
    return counter() if callable(counter) else NOT_CAPTURED


def flow_state(container, viewer: Viewer, case_id: str) -> dict[str, Any]:
    """The named transition table: exactly the fields worth watching move.

    Fields the implementation does not store are labelled as such rather
    than filled in with a plausible value.
    """
    deps = container.deps
    case = deps.case_repo.get(case_id)
    in_force = deps.form_repo.latest_approved(case_id)
    records = deps.hris_record_repo.list_for_case(case_id)
    approvals = deps.approval_repo.list_for_case(case_id)
    return {
        "case.stage": container.service.get_status(viewer, case_id).stage.value
        + "  "
        + derived("case_status.derive_stage"),
        "case.status (stored)": case.status.value,
        "case.current_form_version": case.current_form_version,
        "form.version_in_force": in_force.version if in_force else "<none>",
        "form.versions": {f"v{f.version}": f.status.value for f in deps.form_repo.list_versions(case_id)},
        "approval.status": {
            f"{a.checkpoint.value}:{a.subject_ref[:28]}": a.status.value for a in approvals
        }
        or "<none raised>",
        "hris.record_status": {
            f"{r.system.value} v{r.form_version}": r.status.value for r in records
        }
        or "<none prepared>",
        "hris.external_ref": {
            f"{r.system.value} v{r.form_version}": r.external_ref or "<none>" for r in records
        }
        or "<none prepared>",
        "hris.employees_in_target_system": employee_count(deps.hris_gateway),
        "hris.written_at": NOT_CAPTURED + " (HrisRecord stores approved_at, but no write timestamp)",
    }


def hris_record_map(deps, case_id: str) -> dict[str, dict[str, Any]]:
    """record_id -> the fields a write can move, for before/after pairing."""
    return {
        r.record_id: {"status": r.status.value, "external_ref": r.external_ref}
        for r in deps.hris_record_repo.list_for_case(case_id)
    }


# The audit actions execute_hris_write appends once per record per gateway
# call. Their presence is evidence a call was made; their absence is
# evidence one was not. Nothing here infers a call from a record's status.
_CALL_RESULT_ACTIONS = {
    "HRIS_WRITE_SUCCEEDED": "yes -- gateway returned, audited HRIS_WRITE_SUCCEEDED",
    "HRIS_WRITE_FAILED": "yes -- gateway raised, audited HRIS_WRITE_FAILED",
}


def trace_hris_batch(
    title: str,
    container,
    case_id: str,
    before_map: dict[str, dict[str, Any]],
    entries: list,
    employees_before: Any,
) -> None:
    """One EXTERNAL OP line per target-system record, each carrying its own
    mode, read off the gateway actually wired in.

    Whether a gateway call happened is taken from the audit entries the
    write appended, never from a record's status and never from the fact
    that an external_ref exists.
    """
    deps = container.deps
    calls: dict[str, str] = {}
    for entry in entries:
        verdict = _CALL_RESULT_ACTIONS.get(entry.action)
        if verdict and entry.after and entry.after.get("record_id"):
            calls[entry.after["record_id"]] = verdict

    operations: list[dict[str, Any]] = []
    retired: list[str] = []
    for record in sorted(
        deps.hris_record_repo.list_for_case(case_id),
        key=lambda r: (r.form_version, r.system.value),
    ):
        previous = before_map.get(record.record_id, {})
        if record.record_id not in calls and record.record_id not in before_map:
            continue
        called = record.record_id in calls
        # A record retired to CORRECTED is bookkeeping, not an attempted
        # call: the employee it pointed at was updated through the newer
        # record. Counting it as "not attempted" would overstate how many
        # operations this batch even considered.
        if not called and record.status.value == "CORRECTED":
            retired.append(
                f"{record.system.value} v{record.form_version} "
                f"{previous.get('status', '<unknown>')} -> CORRECTED "
                f"(ref {record.external_ref} is now carried by the newer record)"
            )
            continue
        operation = (
            f"gateway.update(external_ref={record.updates_external_ref})"
            if record.updates_external_ref
            else "gateway.write()"
        )
        operations.append(
            {
                "_called": called,
                "system": record.system.value,
                "operation": operation if called else f"{operation} -- NOT CALLED",
                "record_id": record.record_id,
                "form_version": record.form_version,
                "idempotency_key": record.idempotency_key,
                "gateway call this attempt": calls.get(
                    record.record_id, "no -- no call result was audited for this record"
                ),
                "record status": f"{previous.get('status', '<new>')} -> {record.status.value}",
                "external_ref": f"{previous.get('external_ref') or '<none>'} -> "
                f"{record.external_ref or '<none>'}",
                "last_error (exception class only)": record.last_error or "<none>",
            }
        )

    TRACER.external_operations(
        title,
        deps.hris_gateway,
        operations,
        employees_before=employees_before,
        employees_after=employee_count(deps.hris_gateway),
    )
    if retired:
        TRACER.emit(
            "STATE CHANGE",
            "records retired to CORRECTED (no gateway call was made for these)",
            data=retired,
        )


def payload_view(deps, record_ids: list[str], form: OnboardingForm) -> list[dict[str, Any]]:
    """What is actually queued for each target system.

    `prepared_payload` is the stored, non-sensitive payload. The salary
    payload is never stored: build_sensitive_payload rebuilds it from the
    in-force form at write time, so only its FIELD NAMES are shown here.
    """
    out = []
    for record_id in record_ids:
        record = deps.hris_record_repo.get(record_id)
        out.append(
            {
                "record_id": record.record_id,
                "system": record.system.value,
                "form_version": record.form_version,
                "status": record.status.value,
                "idempotency_key": record.idempotency_key,
                "updates_external_ref": record.updates_external_ref,
                "prepared_payload": record.prepared_payload,
                "sensitive_field_names (names only, values never stored)": record.sensitive_field_names,
                "sensitive_payload_fields_at_write_time": sorted(
                    build_sensitive_payload(record.system, form).keys()
                ),
            }
        )
    return out


# =========================================================================
# the shared end-to-end case: one offer traced from intake to final status
# =========================================================================


def run_main_flow(container) -> None:
    svc = container.service
    deps = container.deps
    intake = container.intake_deps
    onboarding = container.onboarding_deps
    tap = AuditTap(deps.audit_log)

    officer = register_actor(
        Viewer(user_id=OFFICER_ID, role=Role.HR_OFFICER, client_ids=frozenset({CLIENT}))
    )
    manager = register_actor(
        Viewer(user_id=MANAGER_ID, role=Role.HR_MANAGER, client_ids=frozenset({CLIENT}))
    )

    def form_fields(**overrides) -> dict[str, Any]:
        fields: dict[str, Any] = dict(
            client_id=CLIENT,
            version=1,
            candidate_name="Taylor Synthetic",
            candidate_email="taylor@example.test",
            designation="Engineer",
            joining_date=date(2026, 4, 1),
            work_location_type=WorkLocationType.OFFICE,
            salary_and_terms=SecretStr(SALARY),
            status=FormStatus.APPROVED,
            approved_by=MANAGER_ID,
            approved_at=datetime.now(timezone.utc),
        )
        fields.update(overrides)
        return fields

    def make_form(**overrides) -> OnboardingForm:
        return OnboardingForm(**form_fields(**overrides))

    def make_offer(ref: str, email: str = "taylor@example.test") -> OfferRecord:
        offer = OfferRecord(
            client_id=CLIENT, offer_ref=ref, candidate_email=email, hr_officer_id=OFFICER_ID
        )
        intake.offer_repo.save(offer)
        return offer

    def acceptance(ref: str, email: str = "taylor@example.test") -> str:
        return f"From: {email}\nSubject: Re: offer [{ref}] - I accept\n\nbody text\n"

    # -- R1-A ----------------------------------------------------------
    scenario(
        "R1-A",
        "R1",
        "An offer acceptance arrives while the internal onboarding form is still unapproved.",
        "The start gate HELDs the request; no case is created and the start is parked as PENDING.",
        "R1: start only after acceptance AND an approved form",
    )
    offer = make_offer("OFFER-1001")
    raw = acceptance("OFFER-1001")
    TRACER.input(
        "seeded offer record (synthetic)",
        data=offer,
    )
    TRACER.input(
        "inbound acceptance email (raw)",
        data={"raw": raw},
        note="only the From and Subject headers are parsed; the body is never read or stored",
    )
    parsed = parse_acceptance_email(raw)
    TRACER.processing(
        "parse_acceptance_email(raw) -> ParsedAcceptance | IntakeRejection",
        fields={
            "parsed_type": type(parsed).__name__,
            "sender_email": getattr(parsed, "sender_email", None),
            "offer_ref": getattr(parsed, "offer_ref", None),
            "subject": getattr(parsed, "subject", None),
        },
    )
    TRACER.input(
        "onboarding form available for this offer at this point",
        data={"form_source.latest_for_offer": intake.form_source.latest_for_offer(CLIENT, "OFFER-1001")},
    )
    TRACER.processing("handle_acceptance_email(raw, intake_deps) -> try_start -> evaluate_start_gate")
    tap.reset()
    held = handle_acceptance_email(raw, intake)
    TRACER.rule(
        "start gate (start_gate.evaluate_start_gate): both conditions required",
        fields={
            "hiring_freeze": False,
            "offer_accepted": True,
            "form present": intake.form_source.latest_for_offer(CLIENT, "OFFER-1001") is not None,
            "form status APPROVED": False,
            "reasons returned": held.reasons,
            "decision": held.outcome.value,
        },
    )
    TRACER.output("IntakeResult", data=held)
    TRACER.audit(tap.new_entries())
    check(
        "R1",
        "acceptance without an approved form -> held",
        held.outcome is IntakeOutcome.WAITING_FOR_FORM,
        held.outcome.value,
        expected=IntakeOutcome.WAITING_FOR_FORM.value,
        actual=held.outcome.value,
        reason="no onboarding form exists for the offer yet, so the start gate holds",
    )

    # -- R1-B ----------------------------------------------------------
    scenario(
        "R1-B",
        "R1",
        "An approved onboarding form exists but the offer has not been accepted.",
        "The start gate HELDs with reason 'offer not accepted'; no case is created.",
        "R1: approved form without acceptance",
    )
    request = OpenCaseRequest(placement=CasePlacement(location_name="Synthetic Office, Dubai"), 
        request_id="rq-noaccept",
        client_id=CLIENT,
        hr_officer_id=OFFICER_ID,
        offer_accepted=False,
        form=make_form(),
    )
    TRACER.input("OpenCaseRequest", data=request.model_dump(exclude={"form"}))
    TRACER.input("form carried on the request", data=form_fields_view(request.form))
    TRACER.processing("open_onboarding_case(request, onboarding_deps)")
    tap.reset()
    no_accept = open_onboarding_case(request, onboarding)
    TRACER.rule(
        "start gate: offer_accepted is the other half of R1",
        fields={
            "offer_accepted": False,
            "form status": request.form.status.value,
            "reasons returned": no_accept.reasons,
            "decision": no_accept.outcome.value,
        },
    )
    TRACER.output("OpenCaseResult", data=no_accept.model_dump(exclude={"case"}))
    TRACER.audit(tap.new_entries())
    check(
        "R1",
        "approved form without acceptance -> held",
        no_accept.outcome.value == "HELD",
        no_accept.outcome.value,
        expected="HELD",
        actual=no_accept.outcome.value,
        reason=f"reasons={no_accept.reasons}",
    )
    check(
        "R1",
        "no case was created while held",
        no_accept.case is None and len(deps.case_repo.list_for_client(CLIENT)) == 0,
        expected="0 cases for the client",
        actual=f"{len(deps.case_repo.list_for_client(CLIENT))} cases",
        reason="a HELD start gate must not create a case",
    )

    # -- R1-C ----------------------------------------------------------
    scenario(
        "R1-C",
        "R1",
        "The form for the already-accepted offer is approved, satisfying both conditions.",
        "handle_form_approved retries the parked start and the case opens.",
        "R1: both conditions met -> case opened",
    )
    approved_form = make_form()
    _, validation = validate_form_fields(form_fields())
    TRACER.input("onboarding form being approved for the offer", data=form_fields_view(approved_form))
    TRACER.processing(
        "OnboardingForm field validation (pydantic model rules, incl. "
        "APPROVED-requires-approver and CLIENT_SITE-requires-site-id)"
    )
    TRACER.emit("BUSINESS RULE", "input validation, field by field", data=validation)
    intake.form_source.save_for_offer(CLIENT, "OFFER-1001", approved_form)
    TRACER.processing("handle_form_approved(client_id, offer_ref, intake_deps) -> try_start")
    tap.reset()
    opened = handle_form_approved(CLIENT, "OFFER-1001", intake)
    case_id = opened.case_id
    TRACER.rule(
        "start gate: both conditions now satisfied",
        fields={
            "offer_accepted": intake.offer_repo.get(CLIENT, "OFFER-1001").status.value == "ACCEPTED",
            "form status": approved_form.status.value,
            "decision": opened.outcome.value,
        },
    )
    TRACER.rule(
        "duplicate/variant check (duplicate_check.check_duplicates) ran before the case was created",
        fields={
            "matched records": [
                e.after for e in tap.new_entries() if e.action == "DUPLICATE_CHECK_EVALUATED"
            ],
        },
    )
    TRACER.output("IntakeResult", data=opened)
    TRACER.state_change("case store after opening", {}, case_snapshot(deps, case_id))
    TRACER.audit(tap.new_entries())
    check(
        "R1",
        "both conditions met -> case opened",
        opened.outcome is IntakeOutcome.CASE_OPENED,
        opened.outcome.value,
        expected=IntakeOutcome.CASE_OPENED.value,
        actual=opened.outcome.value,
        reason="offer ACCEPTED and form APPROVED",
    )

    # -- R2 ------------------------------------------------------------
    scenario(
        "R2",
        "R2",
        "The case must be built from the APPROVED form version, not a draft.",
        "Case carries the approved joining date, form v1 is APPROVED, an employee number is assigned.",
        "R2: case created from the approved details",
    )
    status = svc.get_status(officer, case_id)
    form = deps.form_repo.latest_approved(case_id)
    TRACER.processing("svc.get_status(officer, case_id); deps.form_repo.latest_approved(case_id)")
    TRACER.output("CaseStatusView", data=status)
    TRACER.output("form in force", data=form_fields_view(form))
    check(
        "R2",
        "case carries the approved joining date",
        status.joining_date == date(2026, 4, 1),
        expected=date(2026, 4, 1).isoformat(),
        actual=status.joining_date.isoformat(),
    )
    check(
        "R2",
        "form in force is APPROVED version 1",
        form.status is FormStatus.APPROVED and form.version == 1,
        expected="APPROVED v1",
        actual=f"{form.status.value} v{form.version}",
    )
    check(
        "R2",
        "an employee number was assigned",
        bool(status.employee_no),
        status.employee_no,
        expected="a non-empty employee number",
        actual=status.employee_no,
    )

    # -- R3 ------------------------------------------------------------
    scenario(
        "R3",
        "R3",
        "Designation, joining date, work location and approved offer terms are held unchanged.",
        "Every field matches the approved form byte for byte, and the salary never renders in a dump.",
        "R3: maintain designation, joining date, work location, offer terms",
    )
    TRACER.rule(
        "the approved form is the source of truth; no field is recalculated",
        fields={
            "designation": form.designation,
            "joining_date": form.joining_date.isoformat(),
            "work_location_type": form.work_location_type.value,
            "salary_and_terms": form.salary_and_terms,
            "salary copied verbatim (no rounding/transformation)": form.salary_and_terms.get_secret_value()
            == SALARY,
        },
    )
    check("R3", "designation held", form.designation == "Engineer", form.designation,
          expected="Engineer", actual=form.designation)
    check("R3", "joining date held", form.joining_date == date(2026, 4, 1),
          expected="2026-04-01", actual=form.joining_date.isoformat())
    check("R3", "work location held", form.work_location_type is WorkLocationType.OFFICE,
          expected="OFFICE", actual=form.work_location_type.value)
    check(
        "R3",
        "approved offer terms held byte-for-byte",
        form.salary_and_terms.get_secret_value() == SALARY,
        expected="stored salary == approved salary",
        actual="match" if form.salary_and_terms.get_secret_value() == SALARY else "MISMATCH",
        reason="compared without printing either value",
    )
    check(
        "R3",
        "offer terms masked in any dump",
        "118000" not in form.model_dump_json(),
        expected="salary figure absent from model_dump_json()",
        actual="absent" if "118000" not in form.model_dump_json() else "PRESENT",
    )

    # -- R6 (before R4: discrepancies are resolved before preparing) ----
    scenario(
        "R6",
        "R6",
        "Another system reports a different candidate name, then a different salary.",
        "Each difference becomes an OPEN discrepancy with an HC_04_DATA approval; the form is never "
        "overwritten and neither salary value reaches the audit log.",
        "R6: flag discrepancies for HR approval",
    )
    name_snap = SourceSnapshot(source="hr_system", values={"candidate_name": "Taylor S. Synthetic"})
    TRACER.input(
        "SourceSnapshot from another system",
        data={"source": name_snap.source, "values": name_snap.values},
    )
    TRACER.processing("svc.compare_sources(officer, case_id, [snapshot])")
    tap.reset()
    before_compare = case_snapshot(deps, case_id)
    first = svc.compare_sources(officer, case_id, [name_snap])
    TRACER.rule(
        "compare_sources: form value vs other-source value, normalised",
        fields={
            "field": "candidate_name",
            "form value": form.candidate_name,
            "other-source value": name_snap.values["candidate_name"],
            "differs": len(first.new_discrepancies) == 1,
            "action taken": "flag for HR; the form is NOT overwritten",
        },
    )
    TRACER.output("CompareResult", data=first)
    TRACER.state_change("discrepancies/approvals after compare", before_compare, case_snapshot(deps, case_id))
    TRACER.audit(tap.new_entries())
    check(
        "R6",
        "a difference becomes a discrepancy",
        len(first.new_discrepancies) == 1,
        expected="1 new discrepancy",
        actual=f"{len(first.new_discrepancies)} new discrepancies",
    )
    check(
        "R6",
        "the form is not overwritten by the other source",
        deps.form_repo.latest_approved(case_id).candidate_name == "Taylor Synthetic",
        expected="Taylor Synthetic (form value unchanged)",
        actual=deps.form_repo.latest_approved(case_id).candidate_name,
    )

    discrepancy_id = first.new_discrepancies[0]
    discrepancy_approval = next(
        a
        for a in deps.approval_repo.list_for_case(case_id)
        if a.subject_ref == f"discrepancy:{discrepancy_id}"
    )
    gate = ApprovalGate(
        container,
        discrepancy_approval,
        why="UC-01 flags differences, it never reconciles them: a human decides whether the form "
        "value stands or the difference is accepted as a change (R6).",
        blocks="prepare_hris returns BLOCKED_OPEN_DISCREPANCIES while any discrepancy is OPEN, "
        "so no HRIS record can be prepared or written, and no Zoho call is attempted.",
        awaiting=deps.discrepancy_repo.get(discrepancy_id),
        blocked_if_rejected="resolve_discrepancy has no reject path: both decisions "
        "(KEEP_FORM_VALUE / ACCEPT_AS_CHANGE) close the approval as APPROVED.",
    )
    before_resolve = flow_state(container, officer, case_id)
    tap.reset()
    svc.resolve_discrepancy(officer, discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE)
    gate.decided(
        effect="discrepancy status -> RESOLVED; the approved form value stands unchanged",
        next_step="the case is no longer blocked by this discrepancy and can be prepared for HRIS",
        resulting_state={
            "discrepancy.status": deps.discrepancy_repo.get(discrepancy_id).status.value,
            "discrepancy.decided_by": deps.discrepancy_repo.get(discrepancy_id).decided_by,
            "form value after the decision": deps.form_repo.latest_approved(case_id).candidate_name,
        },
    )
    TRACER.transition(
        "flow state across the discrepancy decision", before_resolve, flow_state(container, officer, case_id)
    )
    TRACER.output("discrepancy after decision", data=deps.discrepancy_repo.get(discrepancy_id))
    TRACER.audit(tap.new_entries())
    check(
        "R6",
        "HR resolves it explicitly",
        deps.discrepancy_repo.get(discrepancy_id).status.value == "RESOLVED",
        "KEEP_FORM_VALUE",
        expected="RESOLVED by a named human",
        actual=f"{deps.discrepancy_repo.get(discrepancy_id).status.value} "
        f"by {deps.discrepancy_repo.get(discrepancy_id).decided_by}",
    )

    salary_snap = SourceSnapshot(source="offer_letter", values={"salary_and_terms": OTHER_SALARY})
    TRACER.input(
        "SourceSnapshot carrying a conflicting salary",
        data={"source": salary_snap.source, "values": salary_snap.values},
        note="the value is masked here and in every record below; only the FACT of a difference is reported",
    )
    tap.reset()
    second = svc.compare_sources(officer, case_id, [salary_snap])
    audit_blob = str([(e.action, e.before, e.after) for e in deps.audit_log.entries()])
    salary_discrepancy = deps.discrepancy_repo.get(second.new_discrepancies[0])
    TRACER.rule(
        "salary comparison: difference reported, neither figure recorded",
        fields={
            "field": "salary_and_terms",
            "differs": len(second.new_discrepancies) == 1,
            "value_form stored on the discrepancy": salary_discrepancy.value_form,
            "value_other stored on the discrepancy": salary_discrepancy.value_other,
            "form figure present in audit log": "118000" in audit_blob,
            "other figure present in audit log": "999999" in audit_blob,
        },
    )
    TRACER.output("CompareResult", data=second)
    TRACER.audit(tap.new_entries())
    check(
        "R6",
        "salary difference flagged without either value leaking",
        len(second.new_discrepancies) == 1
        and "999999" not in audit_blob
        and "118000" not in audit_blob,
        expected="1 discrepancy, 0 salary figures in the audit log",
        actual=f"{len(second.new_discrepancies)} discrepancies, "
        f"form figure in log={'118000' in audit_blob}, other figure in log={'999999' in audit_blob}",
    )
    for discrepancy_id in second.new_discrepancies:
        svc.resolve_discrepancy(officer, discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE)

    # -- R4 ------------------------------------------------------------
    scenario(
        "R4",
        "R4",
        "Prepare one record per target system, gate the write on a human decision, then write.",
        "Two PREPARED records (no salary stored), one PENDING HC_05 approval, write blocked until "
        "approved, then both records WRITTEN with external references.",
        "R4: prepare and write Zoho People and Zoho Payroll",
    )
    TRACER.input(
        "field mapping used to build the payloads (hris_mapping.HRIS_FIELD_MAPPING)",
        data={
            system.value: [
                {"our_field": f.our_field, "target_field": f.target_field, "sensitive": f.sensitive}
                for f in HRIS_FIELD_MAPPING[system]
            ]
            for system in HRIS_SYSTEMS
        },
    )
    TRACER.processing("svc.prepare_hris(officer, case_id) -> hris_prepare.prepare_hris_records")
    tap.reset()
    before_prepare = case_snapshot(deps, case_id)
    prepared = svc.prepare_hris(officer, case_id)
    records = [deps.hris_record_repo.get(rid) for rid in prepared.record_ids]
    systems = sorted(r.system.value for r in records)
    TRACER.rule(
        "prepare blockers evaluated in order (all clear here)",
        fields={
            "case active": deps.case_repo.get(case_id).status.value,
            "open discrepancies": len(
                [d for d in deps.discrepancy_repo.list_for_case(case_id) if d.status.value == "OPEN"]
            ),
            "pending variant decisions": len(
                [
                    a
                    for a in deps.approval_repo.list_for_case(case_id)
                    if a.checkpoint is Checkpoint.HC_12_VARIANT
                    and a.status is ApprovalStatus.PENDING
                    and a.subject_ref != "possible_duplicate"
                ]
            ),
            "form change awaiting approval": any(
                f.status is FormStatus.AWAITING_APPROVAL for f in deps.form_repo.list_versions(case_id)
            ),
            "outcome": prepared.outcome.value,
        },
    )
    TRACER.output("PrepareResult", data=prepared)
    TRACER.output(
        "payloads prepared for the target systems",
        data=payload_view(deps, prepared.record_ids, deps.form_repo.latest_approved(case_id)),
    )
    TRACER.state_change("HRIS records after prepare", before_prepare, case_snapshot(deps, case_id))
    TRACER.audit(tap.new_entries())
    check(
        "R4",
        "both target systems prepared",
        systems == ["ZOHO_PAYROLL", "ZOHO_PEOPLE"],
        str(systems),
        expected="['ZOHO_PAYROLL', 'ZOHO_PEOPLE']",
        actual=str(systems),
    )
    check(
        "R4",
        "salary never stored in a prepared payload",
        all("118000" not in str(r.prepared_payload) for r in records),
        expected="0 payloads containing the salary figure",
        actual=f"{sum('118000' in str(r.prepared_payload) for r in records)} payloads containing it",
        reason="sensitive fields are rebuilt at write time, never persisted on HrisRecord",
    )

    write_approvals = [
        a
        for a in deps.approval_repo.list_for_case(case_id)
        if a.checkpoint.value == "HC_05_HRIS_WRITE" and a.status.value == "PENDING"
    ]
    write_gate = ApprovalGate(
        container,
        write_approvals[0],
        why="A write to a system of record is never automatic: CLAUDE.md requires a person's approval "
        "before anything is written to Zoho People or Zoho Payroll.",
        blocks="execute_hris_write refuses while the records are still PREPARED "
        "('HRIS records ... are not yet approved for write'), so no gateway call is made at all.",
        awaiting=payload_view(deps, prepared.record_ids, deps.form_repo.latest_approved(case_id)),
        blocked_if_rejected="records move to REJECTED and re-preparing the same form version "
        "returns BLOCKED_HRIS_REJECTED (demonstrated in scenario REJ-1).",
    )
    check(
        "R4",
        "the write waits on a human approval",
        len(write_approvals) == 1 and prepared.approval_id is not None,
        f"approval_id={prepared.approval_id}",
        expected="exactly 1 PENDING HC_05_HRIS_WRITE approval",
        actual=f"{len(write_approvals)} pending, approval_id={prepared.approval_id}",
    )

    # Demonstrate the block rather than asserting it from a table.
    TRACER.processing("svc.execute_hris_write(manager, case_id) ATTEMPTED BEFORE APPROVAL")
    blocked_error: Optional[str] = None
    blocked_error_class: Optional[str] = None
    employees_before_block = employee_count(deps.hris_gateway)
    try:
        svc.execute_hris_write(manager, case_id)
    except ValueError as exc:
        blocked_error_class = type(exc).__name__
        blocked_error = str(exc)
    TRACER.emit(
        "ERROR" if blocked_error else "OUTPUT",
        "attempt to write while the approval is still PENDING",
        fields={
            "exception class": blocked_error_class or "<nothing raised>",
            "message (redacted, raised by execute_hris_write)": blocked_error or "<none>",
            "records still at": sorted({r.status.value for r in records}),
        },
    )
    TRACER.external_op_not_attempted(
        "Zoho People / Zoho Payroll write",
        deps.hris_gateway,
        why="execute_hris_write raised before reaching the gateway: the HC_05 approval is still "
        f"PENDING. Employees in the target system unchanged at {employees_before_block}.",
    )
    check(
        "R4",
        "writing before approval is refused",
        blocked_error is not None and "not yet approved" in blocked_error,
        expected="ValueError mentioning 'not yet approved'",
        actual=blocked_error or "no error raised",
    )

    before_decide = flow_state(container, officer, case_id)
    tap.reset()
    svc.decide_hris_write(manager, prepared.approval_id, approve=True)
    write_gate.decided(
        effect="both HrisRecords move PREPARED -> APPROVED and carry approved_by/approved_at",
        next_step="execute_hris_write may now call the gateway, people record before payroll",
        resulting_state={
            f"{r.system.value} record status": r.status.value
            for r in deps.hris_record_repo.list_for_case(case_id)
        },
    )
    TRACER.transition(
        "flow state across the HC_05 decision", before_decide, flow_state(container, officer, case_id)
    )
    TRACER.audit(tap.new_entries())

    TRACER.processing(
        "svc.execute_hris_write(manager, case_id) -- people record before payroll (WRITE_ORDER)",
        fields={"writes_enabled (kill switch)": deps.hris_writes_enabled()},
    )
    before_write_state = flow_state(container, officer, case_id)
    before_write_map = hris_record_map(deps, case_id)
    employees_before_write = employee_count(deps.hris_gateway)
    tap.reset()
    written = svc.execute_hris_write(manager, case_id)
    write_entries = tap.new_entries()
    TRACER.output("WriteOutcome", data=written)
    trace_hris_batch(
        "Zoho People / Zoho Payroll -- initial create, one line per record",
        container,
        case_id,
        before_write_map,
        write_entries,
        employees_before_write,
    )
    TRACER.transition(
        "flow state across the write", before_write_state, flow_state(container, officer, case_id)
    )
    final_records = deps.hris_record_repo.list_for_case(case_id)
    TRACER.audit(write_entries)
    check(
        "R4",
        "write completed to both systems",
        written.completed and len(written.written) == 2,
        f"written={len(written.written)} failed={len(written.failed)}",
        expected="completed=True, 2 records written",
        actual=f"completed={written.completed}, written={len(written.written)}, failed={len(written.failed)}",
    )
    check(
        "R4",
        "each written record has an external reference",
        len(final_records) == 2 and all(r.external_ref for r in final_records),
        expected="2 records, each with an external_ref",
        actual=f"{len(final_records)} records, "
        f"{sum(1 for r in final_records if r.external_ref)} with an external_ref",
        reason=f"external references are {external_mode(deps.hris_gateway)}",
    )

    # -- R5 ------------------------------------------------------------
    scenario(
        "R5-A",
        "R5",
        "The same acceptance email arrives a second time.",
        "The offer is already ACCEPTED, so intake reports DUPLICATE_ACCEPTANCE and opens no second case.",
        "R5: duplicates, rehires, internal transfers, detail changes",
    )
    TRACER.input("repeat acceptance email", data={"raw": acceptance("OFFER-1001")})
    TRACER.rule(
        "intake duplicate rule",
        fields={"offer status before": intake.offer_repo.get(CLIENT, "OFFER-1001").status.value},
    )
    cases_before = len(deps.case_repo.list_for_client(CLIENT))
    tap.reset()
    duplicate = handle_acceptance_email(acceptance("OFFER-1001"), intake)
    TRACER.output("IntakeResult", data=duplicate)
    TRACER.audit(tap.new_entries())
    check(
        "R5",
        "duplicate request does not open a second case",
        duplicate.outcome is IntakeOutcome.DUPLICATE_ACCEPTANCE
        and len(deps.case_repo.list_for_client(CLIENT)) == cases_before,
        duplicate.outcome.value,
        expected=f"DUPLICATE_ACCEPTANCE, case count stays {cases_before}",
        actual=f"{duplicate.outcome.value}, case count {len(deps.case_repo.list_for_client(CLIENT))}",
    )

    scenario(
        "R5-B",
        "R5",
        "A joiner whose email matches a PAST_EMPLOYEE record in the person directory.",
        "check_duplicates returns REHIRE, the case carries is_rehire, and an HC_12 variant approval is raised.",
        "R5: rehire detected",
    )
    rehire_person = PersonRecord(
        client_id=CLIENT,
        email="rehire@example.test",
        full_name="Jordan Rehire",
        kind=PersonRecordKind.PAST_EMPLOYEE,
    )
    onboarding.person_directory.add(rehire_person)
    TRACER.input("existing person record in the directory", data=rehire_person)
    tap.reset()
    rehire = open_onboarding_case(
        OpenCaseRequest(placement=CasePlacement(location_name="Synthetic Office, Dubai"), 
            request_id="rq-rehire",
            client_id=CLIENT,
            hr_officer_id=OFFICER_ID,
            offer_accepted=True,
            form=make_form(candidate_email="rehire@example.test", candidate_name="Jordan Rehire"),
        ),
        onboarding,
    )
    TRACER.rule(
        "duplicate_check match priority: OPEN_CASE > CURRENT_EMPLOYEE > PAST_EMPLOYEE",
        fields={
            "matched kind": PersonRecordKind.PAST_EMPLOYEE.value,
            "outcome": [
                e.after for e in tap.new_entries() if e.action == "DUPLICATE_CHECK_EVALUATED"
            ],
            "variants set": {k: v for k, v in rehire.case.variants.model_dump().items() if v},
        },
    )
    TRACER.output("OpenCaseResult", data=rehire.model_dump(exclude={"case"}))
    TRACER.output("case opened", data=rehire.case)
    TRACER.audit(tap.new_entries())
    check(
        "R5",
        "rehire detected and flagged",
        rehire.case is not None and rehire.case.variants.is_rehire,
        expected="is_rehire=True",
        actual=f"is_rehire={rehire.case.variants.is_rehire if rehire.case else None}",
    )

    scenario(
        "R5-C",
        "R5",
        "A joiner whose email matches a CURRENT_EMPLOYEE record.",
        "check_duplicates returns INTERNAL_TRANSFER and the case carries is_internal_transfer.",
        "R5: internal transfer detected",
    )
    transfer_person = PersonRecord(
        client_id=CLIENT,
        email="xfer@example.test",
        full_name="Sam Transfer",
        kind=PersonRecordKind.CURRENT_EMPLOYEE,
    )
    onboarding.person_directory.add(transfer_person)
    TRACER.input("existing person record in the directory", data=transfer_person)
    tap.reset()
    transfer = open_onboarding_case(
        OpenCaseRequest(placement=CasePlacement(location_name="Synthetic Office, Dubai"), 
            request_id="rq-transfer",
            client_id=CLIENT,
            hr_officer_id=OFFICER_ID,
            offer_accepted=True,
            form=make_form(candidate_email="xfer@example.test", candidate_name="Sam Transfer"),
        ),
        onboarding,
    )
    TRACER.output("case opened", data=transfer.case)
    TRACER.audit(tap.new_entries())
    check(
        "R5",
        "internal transfer detected and flagged",
        transfer.case is not None and transfer.case.variants.is_internal_transfer,
        expected="is_internal_transfer=True",
        actual=f"is_internal_transfer={transfer.case.variants.is_internal_transfer if transfer.case else None}",
    )

    scenario(
        "R5-D",
        "R5",
        "The joiner asks to move the start date after the case already exists.",
        "A new form version 2 is created AWAITING_APPROVAL, blocked behind HC_01_FORM; once approved "
        "it supersedes v1 and carries the unchanged salary forward verbatim.",
        "R5: employee detail change",
    )
    change_request = FormChangeRequest(
        case_id=case_id,
        joining_date=date(2026, 5, 1),
        change_reason="joiner asked to move the start date",
        requested_by=OFFICER_ID,
    )
    TRACER.input("FormChangeRequest", data=change_request)
    TRACER.input("form in force before the change", data=form_fields_view(deps.form_repo.latest_approved(case_id)))
    before_change = case_snapshot(deps, case_id)
    tap.reset()
    proposed = svc.propose_form_change(officer, change_request, approver_id=MANAGER_ID)
    TRACER.processing("svc.propose_form_change(officer, request, approver_id) -> form_changes.propose_form_change")
    TRACER.rule(
        "only fields that actually differ become a change",
        fields={
            "changed_fields": proposed.changed_fields,
            "warnings (weekend/holiday/past-date)": proposed.warnings or "<none>",
            "new version": proposed.new_version,
        },
    )
    TRACER.output("ProposeResult", data=proposed)
    TRACER.state_change("form versions after proposing", before_change, case_snapshot(deps, case_id))
    TRACER.audit(tap.new_entries())
    check(
        "R5",
        "detail change creates a version awaiting approval",
        proposed.new_version == 2,
        f"v{proposed.new_version}",
        expected="version 2",
        actual=f"version {proposed.new_version}",
    )

    form_approvals = [
        a
        for a in deps.approval_repo.list_for_case(case_id)
        if a.checkpoint.value == "HC_01_FORM" and a.status.value == "PENDING"
    ]
    form_gate = ApprovalGate(
        container,
        form_approvals[0],
        why="A change to approved employee details is a change to the source of truth, so it needs "
        "its own human approval before it takes effect (R5).",
        blocks="prepare_hris returns BLOCKED_FORM_CHANGE_PENDING while a version is AWAITING_APPROVAL, "
        "the in-force form stays at version 1, and no Zoho call is attempted.",
        awaiting=form_fields_view(
            next(f for f in deps.form_repo.list_versions(case_id) if f.version == 2)
        ),
        blocked_if_rejected="form_changes.reject_form_change marks the new version REJECTED and "
        "leaves v1 in force; not exercised by this run.",
    )
    check(
        "R5",
        "the change waits on a human approval",
        len(form_approvals) == 1,
        expected="exactly 1 PENDING HC_01_FORM approval",
        actual=f"{len(form_approvals)} pending",
    )
    check(
        "R5",
        "the in-force version is unchanged while the change is pending",
        deps.form_repo.latest_approved(case_id).version == 1,
        expected="version 1 still in force",
        actual=f"version {deps.form_repo.latest_approved(case_id).version} in force",
    )

    before_form_decision = flow_state(container, officer, case_id)
    before_form_snapshot = case_snapshot(deps, case_id)
    tap.reset()
    svc.decide_form_change(manager, form_approvals[0].approval_id, approve=True)
    in_force = deps.form_repo.latest_approved(case_id)
    form_gate.decided(
        effect="v2 -> APPROVED, v1 -> SUPERSEDED, case.current_form_version -> 2; HRIS records "
        "prepared against v1 that were not written are marked STALE",
        next_step="prepare_hris again to push the changed details to the already-written employees",
        resulting_state={
            "form.version_in_force": in_force.version,
            "v1 status": deps.form_repo.get(case_id, 1).status.value,
            "case.current_form_version": deps.case_repo.get(case_id).current_form_version,
        },
    )
    TRACER.transition(
        "flow state across the HC_01 decision", before_form_decision, flow_state(container, officer, case_id)
    )
    TRACER.state_change(
        "full record diff across the approval",
        before_form_snapshot,
        case_snapshot(deps, case_id),
    )
    TRACER.output("form now in force", data=form_fields_view(in_force))
    TRACER.audit(tap.new_entries())
    check(
        "R5",
        "approved change supersedes version 1",
        in_force.version == 2 and in_force.joining_date == date(2026, 5, 1),
        expected="v2 in force with joining_date 2026-05-01",
        actual=f"v{in_force.version} with joining_date {in_force.joining_date.isoformat()}",
    )
    check(
        "R5",
        "unchanged offer terms carried forward verbatim",
        in_force.salary_and_terms.get_secret_value() == SALARY,
        expected="v2 salary identical to v1 salary",
        actual="identical" if in_force.salary_and_terms.get_secret_value() == SALARY else "CHANGED",
        reason="compared without printing either value",
    )
    check(
        "R5",
        "superseded version 1 is still readable",
        deps.form_repo.get(case_id, 1).status is FormStatus.SUPERSEDED,
        expected="v1 readable with status SUPERSEDED",
        actual=f"v1 status {deps.form_repo.get(case_id, 1).status.value}",
    )

    # -- R4/R3: the change reaches the target system ---------------------
    scenario(
        "R4-UPDATE",
        "R4",
        "The approved change must reach the employees already written, not create new ones.",
        "prepare returns PREPARED_UPDATE with updates_external_ref set; after approval the gateway "
        "update() path runs and the distinct-employee count stays at 2.",
        "R4/R3: an approved change after the write reaches Zoho",
    )
    tap.reset()
    before_reprepare = case_snapshot(deps, case_id)
    reprepared = svc.prepare_hris(officer, case_id)
    update_records = [deps.hris_record_repo.get(r) for r in reprepared.record_ids]
    TRACER.rule(
        "a WRITTEN record at an older form version turns this batch into an in-place update",
        fields={
            "written at older version": [
                f"{r.system.value} v{r.form_version} -> {r.external_ref}"
                for r in deps.hris_record_repo.list_for_case(case_id)
                if r.status.value == "WRITTEN" and r.form_version < 2
            ],
            "outcome": reprepared.outcome.value,
            "updates_external_ref set on new records": [r.updates_external_ref for r in update_records],
        },
    )
    TRACER.output("PrepareResult", data=reprepared)
    TRACER.output(
        "update payloads",
        data=payload_view(deps, reprepared.record_ids, deps.form_repo.latest_approved(case_id)),
    )
    TRACER.state_change("records after re-prepare", before_reprepare, case_snapshot(deps, case_id))
    TRACER.audit(tap.new_entries())
    check(
        "R4",
        "post-write change prepares an in-place update",
        reprepared.outcome.value == "PREPARED_UPDATE",
        reprepared.outcome.value,
        expected="PREPARED_UPDATE",
        actual=reprepared.outcome.value,
    )
    check(
        "R4",
        "the update targets the employees already written",
        all(r.updates_external_ref for r in update_records),
        expected="every new record carries updates_external_ref",
        actual=f"{sum(1 for r in update_records if r.updates_external_ref)}/{len(update_records)} carry it",
    )

    update_gate = ApprovalGate(
        container,
        deps.approval_repo.get(reprepared.approval_id),
        why="An update to a system of record is a write: it needs the same HC_05 human approval as "
        "the original create.",
        blocks="execute_hris_write refuses while the records are PREPARED; the already-written "
        "employees keep their v1 values until this is approved.",
        awaiting=payload_view(deps, reprepared.record_ids, deps.form_repo.latest_approved(case_id)),
        blocked_if_rejected="records move to REJECTED and the target system keeps the v1 values.",
    )
    tap.reset()
    svc.decide_hris_write(manager, reprepared.approval_id, approve=True)
    update_gate.decided(
        effect="v2 records -> APPROVED",
        next_step="execute_hris_write calls gateway.update() (not write()) for each system",
        resulting_state={
            f"{r.system.value} v{r.form_version} status": r.status.value
            for r in deps.hris_record_repo.list_for_case(case_id)
        },
    )
    before_update_state = flow_state(container, officer, case_id)
    before_update_map = hris_record_map(deps, case_id)
    employees_before_update = employee_count(deps.hris_gateway)
    tap.reset()
    update_out = svc.execute_hris_write(manager, case_id)
    update_entries = tap.new_entries()
    TRACER.output("WriteOutcome", data=update_out)
    trace_hris_batch(
        "Zoho People / Zoho Payroll -- in-place update, one line per record",
        container,
        case_id,
        before_update_map,
        update_entries,
        employees_before_update,
    )
    TRACER.emit(
        "EXTERNAL OP",
        "duplicate-prevention evidence",
        fields={
            "distinct employees before -> after": f"{employees_before_update} -> "
            f"{employee_count(deps.hris_gateway)}",
            "gateway.update() calls": deps.hris_gateway.updated_count(),
            "why no duplicate": "update() returns the external_ref it was given and never mints a "
            "new one, so the distinct-employee count cannot rise",
        },
    )
    TRACER.transition(
        "flow state across the update", before_update_state, flow_state(container, officer, case_id)
    )
    TRACER.audit(update_entries)
    check(
        "R4",
        "the update completes",
        update_out.completed,
        expected="completed=True",
        actual=f"completed={update_out.completed}, failed={len(update_out.failed)}",
    )
    check(
        "R4",
        "no second employee was created in either system",
        deps.hris_gateway.written_count() == 2,
        f"employees={deps.hris_gateway.written_count()}",
        expected="2 distinct employees",
        actual=f"{deps.hris_gateway.written_count()} distinct employees",
    )
    live = [r for r in deps.hris_record_repo.list_for_case(case_id) if r.status.value == "WRITTEN"]
    TRACER.output(
        "live records after the update",
        data=[
            {
                "system": r.system.value,
                "form_version": r.form_version,
                "status": r.status.value,
                "external_ref": r.external_ref,
                "start_date in payload": r.prepared_payload.get("start_date"),
            }
            for r in live
        ],
    )
    check(
        "R3",
        "the live record now carries the changed joining date",
        len(live) == 2 and all(r.prepared_payload["start_date"] == "2026-05-01" for r in live),
        f"live={len(live)}",
        expected="2 live records, both with start_date 2026-05-01",
        actual=f"{len(live)} live records, start_dates="
        f"{sorted({r.prepared_payload.get('start_date') for r in live})}",
    )

    # -- R5: a rehire reactivates rather than duplicating ----------------
    scenario(
        "R5-E",
        "R5",
        "A past employee with a known HRIS reference comes back (EC-43).",
        "The case proposes reactivating that existing person, and HR is asked to approve the "
        "reactivation explicitly rather than a second employee being created.",
        "R5: a rehire reactivates the existing employee (EC-43)",
    )
    returner = PersonRecord(
        client_id=CLIENT,
        email="returner@example.test",
        full_name="Jordan Returning",
        kind=PersonRecordKind.PAST_EMPLOYEE,
        hris_external_ref="ext-prior-0001",
    )
    onboarding.person_directory.add(returner)
    TRACER.input("past employee already known to the target system", data=returner)
    tap.reset()
    returning = open_onboarding_case(
        OpenCaseRequest(placement=CasePlacement(location_name="Synthetic Office, Dubai"), 
            request_id="rq-return",
            client_id=CLIENT,
            hr_officer_id=OFFICER_ID,
            offer_accepted=True,
            form=make_form(
                candidate_email="returner@example.test", candidate_name="Jordan Returning"
            ),
        ),
        onboarding,
    )
    TRACER.output("case opened", data=returning.case)
    TRACER.audit(tap.new_entries())
    check(
        "R5",
        "the rehire case proposes reactivating the existing person",
        returning.case is not None and returning.case.reactivates_external_ref == "ext-prior-0001",
        expected="reactivates_external_ref == 'ext-prior-0001'",
        actual=returning.case.reactivates_external_ref if returning.case else None,
    )
    rehire_approvals = [
        a
        for a in onboarding.approval_repo.list_for_case(returning.case.case_id)
        if a.subject_ref == "is_rehire"
    ]
    if rehire_approvals:
        ApprovalGate(
            container,
            rehire_approvals[0],
            why="UC-01 only PROPOSES reactivation; whether this really is the same person, and "
            "whether to reuse their employee record, is HR's call (EC-43).",
            blocks="prepare_hris returns BLOCKED_PENDING_VARIANT_DECISION while this is PENDING, "
            "so nothing can be written for this case.",
            awaiting={
                "proposed action": rehire_approvals[0].reason,
                "person record": returning.case.reactivates_record_id,
                "existing external ref": returning.case.reactivates_external_ref,
            },
            blocked_if_rejected="variants.decide_variant_approval puts the case ON_HOLD on "
            "rejection; not exercised by this run.",
        )
        TRACER.external_op_not_attempted(
            "Zoho People / Zoho Payroll write for the rehire case",
            deps.hris_gateway,
            why="this approval is left PENDING on purpose -- the scenario checks that HR is asked, "
            "not that the write happens. No record was prepared and no gateway call was made.",
        )
    check(
        "R5",
        "HR is asked to approve the reactivation explicitly",
        len(rehire_approvals) == 1 and "reactivat" in (rehire_approvals[0].reason or ""),
        expected="1 HC_12 approval whose reason names reactivation",
        actual=f"{len(rehire_approvals)} approvals, reason="
        f"{rehire_approvals[0].reason if rehire_approvals else None}",
    )

    # -- expected output -----------------------------------------------
    scenario(
        "OUT",
        "OUT",
        "The traced offer ends as one case with a reliable source record and a traceable status.",
        "Exactly one case, version 2 in force, an ordered audit trail, and no salary anywhere in it.",
        "Expected output: one case, reliable source record, traceable status",
    )
    for_offer = [c for c in deps.case_repo.list_for_client(CLIENT) if c.case_id == case_id]
    final = svc.get_status(officer, case_id)
    trail = svc.get_audit_trail(officer, case_id)
    TRACER.output("final CaseStatusView", data=final)
    TRACER.audit(trail, title="full audit trail for the case")
    check(
        "OUT",
        "exactly one case for the offer",
        len(for_offer) == 1,
        expected="1 case with this case_id",
        actual=f"{len(for_offer)} cases",
    )
    check(
        "OUT",
        "status reports the version in force",
        final.form_version_in_force == 2,
        f"stage={final.stage.value}",
        expected="form_version_in_force == 2",
        actual=f"{final.form_version_in_force} (stage={final.stage.value})",
    )
    check(
        "OUT",
        "audit trail present and in order",
        len(trail) > 5 and trail == sorted(trail, key=lambda e: e.timestamp),
        f"{len(trail)} entries",
        expected="more than 5 entries, ascending by timestamp",
        actual=f"{len(trail)} entries, ordered="
        f"{trail == sorted(trail, key=lambda e: e.timestamp)}",
    )
    blob = str([(e.action, e.before, e.after) for e in deps.audit_log.entries()])
    check(
        "OUT",
        "no salary value anywhere in the audit log",
        "118000" not in blob and "999999" not in blob,
        expected="neither salary figure present in the audit log",
        actual=f"form figure present={'118000' in blob}, other figure present={'999999' in blob}",
    )


# =========================================================================
# additional scenarios, each on its own isolated container
# =========================================================================


def new_container():
    return _with_placement(build_container(Settings(app_env="dev")))


def run_validation_scenario() -> None:
    """A missing or invalid field never reaches a case.

    Two inputs, because pydantic short-circuits: field-level errors are
    raised before the model-level validators ever run, so a form missing
    a required field never reaches the CLIENT_SITE rule. Showing both
    separately is the only honest way to report what each one rejects.
    """
    scenario(
        "VAL-1a",
        "R2",
        "An onboarding form is submitted with a required field missing (candidate_email).",
        "Field-level validation rejects it; no form object and so no case can be built from it.",
        "VAL-1a: a missing required field is rejected at validation",
    )
    missing_fields: dict[str, Any] = {
        "client_id": CLIENT,
        "version": 1,
        "candidate_name": "Robin Synthetic",
        # candidate_email deliberately omitted
        "designation": "Engineer",
        "joining_date": date(2026, 7, 1),
        "work_location_type": WorkLocationType.OFFICE,
        "salary_and_terms": SecretStr(SALARY),
        "status": FormStatus.APPROVED,
        "approved_by": "hr-manager-seed",
        "approved_at": datetime.now(timezone.utc),
    }
    TRACER.input("submitted form fields (synthetic)", data=missing_fields)
    TRACER.processing("OnboardingForm(**fields) -- pydantic field validators")
    form, report = validate_form_fields(missing_fields)
    TRACER.emit("BUSINESS RULE", "input validation, field by field", data=report)
    invalid = {name for name, r in report.items() if not r["valid"]}
    TRACER.output(
        "validation verdict",
        fields={"form constructed": form is not None, "fields rejected": sorted(invalid)},
    )
    check(
        "R2",
        "a form missing a required field is rejected before a case exists",
        form is None and "candidate_email" in invalid,
        expected="construction fails, candidate_email reported missing",
        actual=f"constructed={form is not None}, rejected={sorted(invalid)}",
    )

    scenario(
        "VAL-1b",
        "R2",
        "Every field is present and individually valid, but the combination is not: a CLIENT_SITE "
        "work location with no client_site_id.",
        "The model-level validator rejects it. _check_rules raises on the FIRST rule it finds "
        "broken, so each model rule needs its own input to be observable.",
        "VAL-1b: an invalid field combination is rejected at validation",
    )
    invalid_combo: dict[str, Any] = {
        **missing_fields,
        "candidate_email": "robin@example.test",
        "work_location_type": WorkLocationType.CLIENT_SITE,  # requires client_site_id
    }
    TRACER.input("submitted form fields (synthetic)", data=invalid_combo)
    TRACER.processing("OnboardingForm(**fields) -- pydantic model_validator(mode='after')")
    combo_form, combo_report = validate_form_fields(invalid_combo)
    TRACER.emit("BUSINESS RULE", "input validation, field by field", data=combo_report)
    messages = " ".join(
        str(e["msg"]) for r in combo_report.values() for e in r.get("errors", [])
    )
    TRACER.output(
        "validation verdict",
        fields={
            "form constructed": combo_form is not None,
            "rules that fired": messages or "<none>",
        },
    )
    check(
        "R2",
        "CLIENT_SITE without a site id is rejected",
        combo_form is None and "client_site_id" in messages,
        expected="construction fails, the CLIENT_SITE rule named in the error",
        actual=f"constructed={combo_form is not None}, messages={messages!r}",
    )

    scenario(
        "VAL-1b2",
        "R2",
        "A form marked APPROVED that carries no approver.",
        "The model-level validator rejects it: an APPROVED form must name who approved it and when, "
        "which is what makes it usable as the source of truth.",
        "VAL-1b2: APPROVED without an approver is rejected",
    )
    no_approver = {
        **missing_fields,
        "candidate_email": "robin@example.test",
        "approved_by": None,
        "approved_at": None,
    }
    TRACER.input("submitted form fields (synthetic)", data=no_approver)
    TRACER.processing("OnboardingForm(**fields) -- pydantic model_validator(mode='after')")
    no_approver_form, no_approver_report = validate_form_fields(no_approver)
    TRACER.emit("BUSINESS RULE", "input validation, field by field", data=no_approver_report)
    approver_messages = " ".join(
        str(e["msg"]) for r in no_approver_report.values() for e in r.get("errors", [])
    )
    TRACER.output(
        "validation verdict",
        fields={
            "form constructed": no_approver_form is not None,
            "rules that fired": approver_messages or "<none>",
        },
    )
    check(
        "R2",
        "an APPROVED form with no approver is rejected",
        no_approver_form is None and "approved_by" in approver_messages,
        expected="construction fails, the approver rule named in the error",
        actual=f"constructed={no_approver_form is not None}, messages={approver_messages!r}",
    )

    scenario(
        "VAL-1c",
        "R2",
        "The same submission with every rejected field corrected.",
        "Validation passes and the form is usable to open a case.",
        "VAL-1c: the corrected form validates",
    )
    good_fields = {
        **invalid_combo,
        "client_site_id": "site-synthetic-1",
    }
    good_form, good_report = validate_form_fields(good_fields)
    TRACER.input("corrected form fields", data=good_fields)
    TRACER.emit("BUSINESS RULE", "input validation, field by field", data=good_report)
    TRACER.output("validated form", data=form_fields_view(good_form) if good_form else None)
    check(
        "R2",
        "the corrected form validates",
        good_form is not None,
        expected="construction succeeds",
        actual=f"constructed={good_form is not None}",
    )

def run_pending_block_scenario() -> None:
    """A PENDING variant approval really does block the HRIS write."""
    container = new_container()
    deps = container.deps
    client_id = "client-block"
    officer_id = "hr-officer-block"
    officer = register_actor(
        Viewer(user_id=officer_id, role=Role.HR_OFFICER, client_ids=frozenset({client_id}))
    )
    tap = AuditTap(deps.audit_log)

    scenario(
        "PEND-1",
        "R5",
        "A declared minor opens a case, raising an HC_12 variant approval that nobody has decided.",
        "prepare_hris is BLOCKED_PENDING_VARIANT_DECISION while the approval is PENDING, and "
        "succeeds once it is approved.",
        "PEND-1: a pending approval blocks the operation behind it",
    )
    declared = VariantFlags(is_minor=True)
    TRACER.input("declared variant flags on the open-case request", data=declared)
    result = open_onboarding_case(
        OpenCaseRequest(placement=CasePlacement(location_name="Synthetic Office, Dubai"), 
            request_id="rq-minor",
            client_id=client_id,
            hr_officer_id=officer_id,
            offer_accepted=True,
            form=OnboardingForm(
                client_id=client_id,
                version=1,
                candidate_name="Alex Synthetic",
                candidate_email="alex@example.test",
                designation="Apprentice",
                joining_date=date(2026, 8, 3),
                work_location_type=WorkLocationType.OFFICE,
                salary_and_terms=SecretStr(SALARY),
                status=FormStatus.APPROVED,
                approved_by="hr-manager-block",
                approved_at=datetime.now(timezone.utc),
            ),
            declared_variants=declared,
        ),
        container.onboarding_deps,
    )
    case_id = result.case.case_id
    approval = pending_approval(deps, case_id, Checkpoint.HC_12_VARIANT, "is_minor")
    variant_gate = ApprovalGate(
        container,
        approval,
        why="A declared minor is a variant HR must decide on before the record is created.",
        blocks="prepare_hris -- demonstrated immediately below, not asserted from a table.",
        awaiting={"subject_ref": approval.subject_ref, "case_id": case_id},
        blocked_if_rejected="variants.decide_variant_approval puts the case ON_HOLD, after which "
        "prepare_hris returns BLOCKED_CASE_NOT_ACTIVE; not exercised by this run.",
    )
    TRACER.processing("svc.prepare_hris(officer, case_id) WHILE the variant approval is PENDING")
    before_blocked = flow_state(container, officer, case_id)
    tap.reset()
    blocked = container.service.prepare_hris(officer, case_id)
    TRACER.output("PrepareResult", data=blocked)
    TRACER.external_op_not_attempted(
        "Zoho People / Zoho Payroll write",
        deps.hris_gateway,
        why=f"prepare_hris returned {blocked.outcome.value} and prepared no record, so there was "
        "nothing to approve and no gateway call to make.",
    )
    TRACER.transition(
        "flow state while the approval is PENDING", before_blocked, flow_state(container, officer, case_id)
    )
    TRACER.audit(tap.new_entries())
    check(
        "R5",
        "a pending variant approval blocks the HRIS prepare",
        blocked.outcome.value == "BLOCKED_PENDING_VARIANT_DECISION"
        and blocked.record_ids == []
        and blocked.blockers == [approval.approval_id],
        expected="BLOCKED_PENDING_VARIANT_DECISION, 0 records prepared",
        actual=f"{blocked.outcome.value}, {len(blocked.record_ids)} records, blockers={blocked.blockers}",
    )

    tap.reset()
    container.service.decide_variant(officer, approval.approval_id, approve=True, note="synthetic: confirmed")
    variant_gate.decided(
        effect="HC_12 approval -> APPROVED; the variant no longer blocks the case",
        next_step="prepare_hris can run",
        resulting_state={
            "case.status": deps.case_repo.get(case_id).status.value,
            "case.variants set": {
                k: v for k, v in deps.case_repo.get(case_id).variants.model_dump().items() if v
            },
        },
    )
    TRACER.transition(
        "flow state across the HC_12 decision", before_blocked, flow_state(container, officer, case_id)
    )
    TRACER.audit(tap.new_entries())
    unblocked = container.service.prepare_hris(officer, case_id)
    TRACER.output("PrepareResult after the decision", data=unblocked)
    check(
        "R5",
        "approving the variant unblocks the prepare",
        unblocked.outcome.value == "PREPARED" and len(unblocked.record_ids) == 2,
        expected="PREPARED, 2 records",
        actual=f"{unblocked.outcome.value}, {len(unblocked.record_ids)} records",
    )

    scenario(
        "REJ-1",
        "R4",
        "The HR manager REJECTS the HC_05 write approval for this prepared batch.",
        "Both records move to REJECTED and re-preparing the same form version is "
        "BLOCKED_HRIS_REJECTED: rejection is terminal for that version.",
        "REJ-1: a rejected approval stops the write",
    )
    manager = register_actor(
        Viewer(user_id="hr-manager-block", role=Role.HR_MANAGER, client_ids=frozenset({client_id}))
    )
    write_approval = deps.approval_repo.get(unblocked.approval_id)
    reject_gate = ApprovalGate(
        container,
        write_approval,
        why="Nothing is written to a system of record without a person's approval.",
        blocks="execute_hris_write, which refuses while the records are PREPARED.",
        awaiting=payload_view(deps, unblocked.record_ids, deps.form_repo.latest_approved(case_id)),
        blocked_if_rejected="prepare_hris for this SAME form version returns BLOCKED_HRIS_REJECTED "
        "-- rejection is terminal for the version; only a new form version can move it on.",
    )
    before_reject = flow_state(container, officer, case_id)
    employees_before_reject = employee_count(deps.hris_gateway)
    tap.reset()
    container.service.decide_hris_write(
        manager, write_approval.approval_id, approve=False, note="synthetic: details not confirmed"
    )
    reject_gate.decided(
        effect="both HrisRecords -> REJECTED; nothing was sent to any target system",
        next_step="the only way forward is a new form version; re-preparing v1 stays blocked",
        resulting_state={
            f"{r.system.value} status": r.status.value
            for r in deps.hris_record_repo.list_for_case(case_id)
        },
    )
    TRACER.external_op_not_attempted(
        "Zoho People / Zoho Payroll write",
        deps.hris_gateway,
        why="the HC_05 approval was REJECTED, so execute_hris_write was never called. Employees in "
        f"the target system unchanged at {employees_before_reject}.",
    )
    TRACER.transition(
        "flow state across the rejection", before_reject, flow_state(container, officer, case_id)
    )
    TRACER.audit(tap.new_entries())
    check(
        "R4",
        "a rejected write approval leaves nothing written",
        all(
            r.status.value == "REJECTED" and r.external_ref is None
            for r in deps.hris_record_repo.list_for_case(case_id)
        )
        and deps.hris_gateway.written_count() == 0,
        expected="all records REJECTED, 0 employees in the target system",
        actual=f"statuses={sorted({r.status.value for r in deps.hris_record_repo.list_for_case(case_id)})}, "
        f"employees={deps.hris_gateway.written_count()}",
    )
    reprepare = container.service.prepare_hris(officer, case_id)
    TRACER.output("PrepareResult after rejection", data=reprepare)
    check(
        "R4",
        "re-preparing a rejected version is blocked",
        reprepare.outcome.value == "BLOCKED_HRIS_REJECTED",
        expected="BLOCKED_HRIS_REJECTED",
        actual=reprepare.outcome.value,
    )


def run_write_failure_scenario() -> None:
    """A failed external write, then a successful retry."""
    container = new_container()
    deps = container.deps
    client_id = "client-fail"
    officer_id = "hr-officer-fail"
    officer_fail = register_actor(
        Viewer(user_id=officer_id, role=Role.HR_OFFICER, client_ids=frozenset({client_id}))
    )
    officer = officer_fail
    manager = register_actor(
        Viewer(user_id="hr-manager-fail", role=Role.HR_MANAGER, client_ids=frozenset({client_id}))
    )
    tap = AuditTap(deps.audit_log)

    scenario(
        "WRITE-1",
        "R4",
        "The target system is unavailable on the first write attempt, then recovers.",
        "The people record goes FAILED with the exception CLASS name only, payroll is not attempted, "
        "and a retry writes both records without creating a duplicate employee.",
        "WRITE-1: a failed external write, then a successful retry",
    )
    result = open_onboarding_case(
        OpenCaseRequest(placement=CasePlacement(location_name="Synthetic Office, Dubai"), 
            request_id="rq-fail",
            client_id=client_id,
            hr_officer_id=officer_id,
            offer_accepted=True,
            form=OnboardingForm(
                client_id=client_id,
                version=1,
                candidate_name="Morgan Synthetic",
                candidate_email="morgan@example.test",
                designation="Engineer",
                joining_date=date(2026, 9, 1),
                work_location_type=WorkLocationType.OFFICE,
                salary_and_terms=SecretStr(SALARY),
                status=FormStatus.APPROVED,
                approved_by="hr-manager-fail",
                approved_at=datetime.now(timezone.utc),
            ),
        ),
        container.onboarding_deps,
    )
    case_id = result.case.case_id
    prepared = container.service.prepare_hris(officer, case_id)
    container.service.decide_hris_write(manager, prepared.approval_id, approve=True)

    deps.hris_gateway.fail_next(1)
    TRACER.input(
        "injected fault on the gateway stand-in",
        fields={
            "fault": "the next gateway call raises HrisUnavailableError",
            "how": "StubHrisGateway.fail_next(1) -- the stand-in's own test hook",
        },
    )
    TRACER.processing("svc.execute_hris_write(manager, case_id) -- first attempt")
    before_attempt_state = flow_state(container, officer_fail, case_id)
    before_attempt_map = hris_record_map(deps, case_id)
    employees_before_attempt = employee_count(deps.hris_gateway)
    tap.reset()
    attempt = container.service.execute_hris_write(manager, case_id)
    attempt_entries = tap.new_entries()
    TRACER.output("WriteOutcome (first attempt)", data=attempt)
    trace_hris_batch(
        "Zoho People / Zoho Payroll -- first attempt, one line per record",
        container,
        case_id,
        before_attempt_map,
        attempt_entries,
        employees_before_attempt,
    )
    failed_records = [
        r for r in deps.hris_record_repo.list_for_case(case_id) if r.status.value == "FAILED"
    ]
    not_attempted = [
        r for r in deps.hris_record_repo.list_for_case(case_id) if r.status.value == "APPROVED"
    ]
    TRACER.error(
        "failure detail and recovery",
        fields={
            "failed operation": "gateway.write() for ZOHO_PEOPLE",
            "exception class (no message is ever stored)": sorted(
                {r.last_error for r in failed_records}
            ),
            "affected record(s)": [f"{r.system.value}:{r.record_id}" for r in failed_records],
            "not attempted after the failure": [
                f"{r.system.value}:{r.record_id}" for r in not_attempted
            ],
            "why": "execute_hris_write breaks out of the loop when the people record fails, so "
            "payroll is never written without a people record",
            "recovery action": "the record stays FAILED and remains writable; calling "
            "execute_hris_write again retries it under the SAME idempotency key",
            "employees in target system": employee_count(deps.hris_gateway),
        },
    )
    TRACER.transition(
        "flow state across the failed attempt",
        before_attempt_state,
        flow_state(container, officer_fail, case_id),
    )
    TRACER.audit(attempt_entries)
    check(
        "R4",
        "a failed write is recorded without completing",
        not attempt.completed and len(attempt.failed) == 1 and len(attempt.written) == 0,
        expected="completed=False, 1 failed, 0 written",
        actual=f"completed={attempt.completed}, failed={len(attempt.failed)}, written={len(attempt.written)}",
    )
    check(
        "R4",
        "the failure reason is a class name, not a message",
        all(r.last_error == "HrisUnavailableError" for r in failed_records),
        expected="last_error == 'HrisUnavailableError'",
        actual=sorted({r.last_error for r in failed_records}),
    )
    check(
        "R4",
        "payroll is not written when the people record failed",
        all(
            r.status.value != "WRITTEN"
            for r in deps.hris_record_repo.list_for_case(case_id)
        ),
        expected="no record WRITTEN",
        actual=sorted({r.status.value for r in deps.hris_record_repo.list_for_case(case_id)}),
    )

    TRACER.processing(
        "svc.execute_hris_write(manager, case_id) -- retry, gateway healthy",
        fields={
            "idempotency keys reused": sorted(
                {r.idempotency_key for r in deps.hris_record_repo.list_for_case(case_id)}
            ),
            "no new approval needed": "the HC_05 approval is still APPROVED; a retry is the same "
            "approved batch, not a new one",
        },
    )
    before_retry_state = flow_state(container, officer_fail, case_id)
    before_retry_map = hris_record_map(deps, case_id)
    employees_before_retry = employee_count(deps.hris_gateway)
    tap.reset()
    retry = container.service.execute_hris_write(manager, case_id)
    retry_entries = tap.new_entries()
    TRACER.output("WriteOutcome (retry)", data=retry)
    trace_hris_batch(
        "Zoho People / Zoho Payroll -- retry, one line per record",
        container,
        case_id,
        before_retry_map,
        retry_entries,
        employees_before_retry,
    )
    TRACER.emit(
        "EXTERNAL OP",
        "duplicate-prevention evidence",
        fields={
            "distinct employees before -> after": f"{employees_before_retry} -> "
            f"{employee_count(deps.hris_gateway)}",
            "why no duplicate": "the failed write never reached the gateway ref table, and the "
            "retry reuses the same (client_id, idempotency_key), so at most one employee can "
            "exist per record",
        },
    )
    TRACER.transition(
        "flow state across the retry", before_retry_state, flow_state(container, officer_fail, case_id)
    )
    TRACER.audit(retry_entries)
    check(
        "R4",
        "the retry completes both systems",
        retry.completed and len(retry.written) == 2,
        expected="completed=True, 2 written",
        actual=f"completed={retry.completed}, written={len(retry.written)}",
    )
    check(
        "R4",
        "the retry creates no duplicate employee",
        deps.hris_gateway.written_count() == 2,
        expected="2 distinct employees",
        actual=f"{deps.hris_gateway.written_count()} distinct employees",
    )


# =========================================================================


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify UC-01 against the mapped business requirements (R1-R6)."
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="print the full step-by-step execution trace instead of one line per check",
    )
    args = parser.parse_args(argv)

    RESULTS.clear()
    ACTOR_ROLES.clear()
    LEDGER.__init__()
    TRACER.enabled = args.verbose
    # Register every salary literal this run handles, so none of them can
    # reach the terminal even if it turns up inside free text.
    REDACTOR.register_secret(SALARY)
    REDACTOR.register_secret(OTHER_SALARY)

    container = _with_placement(build_container(Settings(app_env="dev")))

    if TRACER.enabled:
        TRACER.heading("UC-01 Employee Record Creation -- detailed verification trace")
        TRACER.emit(
            "INPUT",
            "run configuration (read from the wiring actually in use)",
            fields={
                "app_env": container.settings.app_env,
                "autonomy_level (policy)": container.policy.autonomy_level,
                "llm advisory enabled": container.settings.llm_enabled,
                "hris writes enabled (kill switch)": container.deps.hris_writes_enabled(),
                "HRIS gateway": type(container.deps.hris_gateway).__name__,
                "external operation mode": external_mode(container.deps.hris_gateway),
                "data": "synthetic only; salary values masked in all output",
            },
        )

    run_main_flow(container)
    run_validation_scenario()
    run_pending_block_scenario()
    run_write_failure_scenario()

    print()
    render_summary(LEDGER, UNVERIFIED_PATHS)

    print("\n" + "=" * 64)
    failures = sum(1 for checks in RESULTS.values() for ok, *_ in checks if not ok)
    for req in sorted(RESULTS):
        total = len(RESULTS[req])
        bad = sum(1 for ok, *_ in RESULTS[req] if not ok)
        print(f"{req}: {total - bad}/{total} checks passed")
    if failures:
        print()
        for req in sorted(RESULTS):
            for ok, label, detail in RESULTS[req]:
                if not ok:
                    print(f"FAILED  {req}  {label}" + (f"  -- {detail}" if detail else ""))
    print()
    print("ALL REQUIREMENTS VERIFIED" if not failures else f"{failures} CHECK(S) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
