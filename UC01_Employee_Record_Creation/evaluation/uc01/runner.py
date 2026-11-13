"""Deterministic end-to-end scenario runner for UC-01. See README.md.

Usage: uv run python -m evaluation.uc01.runner
"""

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from sub_agent.adapters.memory import (
    InMemoryApprovalRepository,
    InMemoryAuditLog,
    InMemoryCaseRepository,
    InMemoryDiscrepancyRepository,
    InMemoryDocumentStore,
    InMemoryEmployeeNumberGenerator,
    InMemoryEventPublisher,
    InMemoryFormRepository,
    InMemoryHolidayCalendar,
    InMemoryHrisRecordRepository,
    InMemoryOfferRepository,
    InMemoryOnboardingFormSource,
    InMemoryPendingStartRepository,
    InMemoryPersonDirectory,
    StubHrisGateway,
)
from sub_agent.compare_sources import DiscrepancyDecision
from sub_agent.llm import FakeLlm, load_all_tasks
from sub_agent.deps import UC01Deps
from sub_agent.intake.email_intake import IntakeDeps, handle_acceptance_email, handle_form_approved
from sub_agent.invariants import check_case_invariants
from sub_agent.models import (
    CasePlacement,
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
from sub_agent.onboarding_entry import OnboardingDeps, OpenCaseRequest, open_onboarding_case
from sub_agent.policy import load_policy
from sub_agent.service import CaseService

FINGERPRINT_KEY = b"uc01-eval-fixed-fingerprint-key"

def _llm_enabled() -> bool:
    """One source of truth with the API: Settings.from_env() loads
    .env, so an eval run honours the same UC01_LLM_ENABLED a
    developer set for the app."""
    from sub_agent.api.settings import Settings

    return Settings.from_env().llm_enabled

FIXED_TODAY = date(2026, 1, 15)

FORM_FIELD_KEYS = (
    "candidate_name",
    "candidate_email",
    "designation",
    "joining_date",
    "work_location_type",
    "salary_and_terms",
    "client_site_id",
)


class Toggle:
    def __init__(self, value: bool = True) -> None:
        self.value = value

    def __call__(self) -> bool:
        return self.value


@dataclass
class Bundle:
    change_deps: UC01Deps
    onboarding_deps: OnboardingDeps
    intake_deps: IntakeDeps
    service: CaseService
    viewers: dict[str, Viewer]
    kill_switch: Toggle


def _make_approved_form(fields: dict[str, Any]) -> OnboardingForm:
    defaults = dict(
        client_id="client-eval-001",
        version=1,
        candidate_name="Eval Candidate",
        candidate_email="eval.candidate@example.test",
        designation="Software Engineer",
        joining_date=date(2026, 3, 2),
        work_location_type=WorkLocationType.OFFICE,
        salary_and_terms="Base: 100000 SYNTHETIC/yr; benefits: standard",
    )
    merged = {**defaults, **fields}
    if isinstance(merged.get("joining_date"), str):
        merged["joining_date"] = date.fromisoformat(merged["joining_date"])
    if isinstance(merged.get("work_location_type"), str):
        merged["work_location_type"] = WorkLocationType(merged["work_location_type"])
    form = OnboardingForm(**merged)
    return form.approve(approved_by="hr-officer-eval-approver", approved_at=datetime.now(timezone.utc))


def _make_form_with_status(fields: dict[str, Any], status: str) -> OnboardingForm:
    defaults = dict(
        client_id="client-eval-001",
        version=1,
        candidate_name="Eval Candidate",
        candidate_email="eval.candidate@example.test",
        designation="Software Engineer",
        joining_date=date(2026, 3, 2),
        work_location_type=WorkLocationType.OFFICE,
        salary_and_terms="Base: 100000 SYNTHETIC/yr; benefits: standard",
    )
    merged = {**defaults, **fields}
    if isinstance(merged.get("joining_date"), str):
        merged["joining_date"] = date.fromisoformat(merged["joining_date"])
    if isinstance(merged.get("work_location_type"), str):
        merged["work_location_type"] = WorkLocationType(merged["work_location_type"])
    form = OnboardingForm(**merged)
    if status == "APPROVED":
        return form.approve(approved_by="hr-officer-eval-approver", approved_at=datetime.now(timezone.utc))
    if status == "AWAITING_APPROVAL":
        return form.submit_for_approval()
    return form


def _build_bundle(setup: dict[str, Any]) -> Bundle:
    case_repo = InMemoryCaseRepository()
    form_repo = InMemoryFormRepository()
    approval_repo = InMemoryApprovalRepository()
    audit_log = InMemoryAuditLog()
    publisher = InMemoryEventPublisher()
    discrepancy_repo = InMemoryDiscrepancyRepository()
    hris_record_repo = InMemoryHrisRecordRepository()
    hris_gateway = StubHrisGateway()
    holiday_calendar = InMemoryHolidayCalendar()
    person_directory = InMemoryPersonDirectory()
    document_store = InMemoryDocumentStore()
    number_generator = InMemoryEmployeeNumberGenerator()
    offer_repo = InMemoryOfferRepository()
    form_source = InMemoryOnboardingFormSource()
    pending_start_repo = InMemoryPendingStartRepository()

    for holiday in setup.get("holidays", []):
        holiday_calendar.add(setup.get("client_id", "client-eval-001"), date.fromisoformat(holiday))

    policy = load_policy()
    kill_switch = Toggle(True)

    change_deps = UC01Deps(
        case_repo=case_repo,
        form_repo=form_repo,
        approval_repo=approval_repo,
        discrepancy_repo=discrepancy_repo,
        hris_record_repo=hris_record_repo,
        hris_gateway=hris_gateway,
        audit_log=audit_log,
        publisher=publisher,
        holiday_calendar=holiday_calendar,
        today=lambda: FIXED_TODAY,
        hris_writes_enabled=kill_switch,
        policy=policy,
    )
    # Same switch the API uses (UC01_LLM_ENABLED). Scenarios must pass
    # either way: the advisory model is never allowed to change an
    # outcome, so running with it on is a real check of that rule.
    llm = FakeLlm(load_all_tasks()) if _llm_enabled() else None

    onboarding_deps = OnboardingDeps(
        case_repo=case_repo,
        form_repo=form_repo,
        approval_repo=approval_repo,
        person_directory=person_directory,
        document_store=document_store,
        number_generator=number_generator,
        event_publisher=publisher,
        audit_log=audit_log,
        llm=llm,
    )
    intake_deps = IntakeDeps(
        offer_repo=offer_repo,
        form_source=form_source,
        pending_repo=pending_start_repo,
        onboarding_deps=onboarding_deps,
        llm=llm,
        # The eval harness is the caller, so it supplies the
        # required location_name (approved 2026-11-04). Left unset,
        # try_start parks the offer instead of inventing one.
        placement=CasePlacement(location_name="Synthetic Office, Dubai"),
    )
    service = CaseService(change_deps, FINGERPRINT_KEY, policy, llm=llm)

    client_id = setup.get("client_id", "client-eval-001")
    hr_officer_id = setup.get("hr_officer_id", "hr-officer-eval-1")
    viewers = {
        "HR_OFFICER": Viewer(user_id=hr_officer_id, role=Role.HR_OFFICER, client_ids=frozenset({client_id})),
        "HR_MANAGER": Viewer(
            user_id="hr-manager-eval-1", role=Role.HR_MANAGER, client_ids=frozenset({client_id})
        ),
        "HR_MANAGER_2": Viewer(
            user_id="hr-manager-eval-2", role=Role.HR_MANAGER, client_ids=frozenset({client_id})
        ),
        "CLIENT_USER": Viewer(
            user_id="client-user-eval", role=Role.CLIENT_USER, client_ids=frozenset({client_id})
        ),
        "AUDITOR": Viewer(user_id="auditor-eval", role=Role.AUDITOR, client_ids=frozenset({client_id})),
        "OTHER_CLIENT": Viewer(
            user_id="other-client-user-eval",
            role=Role.CLIENT_USER,
            client_ids=frozenset({"client-other-eval-999"}),
        ),
    }

    return Bundle(change_deps, onboarding_deps, intake_deps, service, viewers, kill_switch)


def _resolve(value: Any, memory: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        parts = value[1:].split(".")
        obj = memory[parts[0]]
        for part in parts[1:]:
            if isinstance(obj, (list, tuple)):
                obj = obj[int(part)]
            elif isinstance(obj, dict):
                obj = obj[part]
            else:
                obj = getattr(obj, part)
        return obj
    if isinstance(value, dict):
        return {k: _resolve(v, memory) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, memory) for v in value]
    return value


def _act_open_case(bundle: Bundle, viewer: Optional[Viewer], args: dict, setup: dict):
    merged = {**setup, **args}
    client_id = merged.get("client_id", "client-eval-001")
    form_fields = {k: merged[k] for k in FORM_FIELD_KEYS if k in merged}
    form_fields["client_id"] = client_id
    form = _make_approved_form(form_fields)
    request = OpenCaseRequest(placement=CasePlacement(location_name="Synthetic Office, Dubai"), 
        request_id=merged.get("request_id", f"req-eval-{uuid4().hex[:8]}"),
        client_id=client_id,
        hr_officer_id=merged.get("hr_officer_id", "hr-officer-eval-1"),
        offer_accepted=merged.get("offer_accepted", True),
        form=form,
        hiring_freeze=merged.get("hiring_freeze", False),
        id_number=merged.get("id_number"),
        declared_variants=VariantFlags(**merged["declared_variants"]) if merged.get("declared_variants") else None,
    )
    return open_onboarding_case(request, bundle.onboarding_deps)


def _act_seed_person_record(bundle: Bundle, viewer: Optional[Viewer], args: dict, setup: dict):
    client_id = args.get("client_id", setup.get("client_id", "client-eval-001"))
    record = PersonRecord(
        client_id=client_id,
        email=args["email"],
        full_name=args["full_name"],
        kind=PersonRecordKind(args["kind"]),
        id_number=args.get("id_number"),
        case_id=args.get("case_id"),
    )
    bundle.onboarding_deps.person_directory.add(record)
    return None


def _act_seed_offer(bundle: Bundle, viewer: Optional[Viewer], args: dict, setup: dict):
    merged = {**setup, **args}
    offer = OfferRecord(
        client_id=merged.get("client_id", "client-eval-001"),
        offer_ref=args["offer_ref"],
        candidate_email=merged.get("candidate_email", "eval.candidate@example.test"),
        hr_officer_id=merged.get("hr_officer_id", "hr-officer-eval-1"),
    )
    bundle.intake_deps.offer_repo.save(offer)
    return None


def _act_seed_form_for_offer(bundle: Bundle, viewer: Optional[Viewer], args: dict, setup: dict):
    merged = {**setup, **args}
    client_id = merged.get("client_id", "client-eval-001")
    form_fields = {k: merged[k] for k in FORM_FIELD_KEYS if k in merged}
    form_fields["client_id"] = client_id
    form = _make_form_with_status(form_fields, args.get("form_status", "DRAFT"))
    bundle.intake_deps.form_source.save_for_offer(client_id, args["offer_ref"], form)
    return None


def _act_approve_offer_form(bundle: Bundle, viewer: Optional[Viewer], args: dict, setup: dict):
    merged = {**setup, **args}
    client_id = merged.get("client_id", "client-eval-001")
    form = bundle.intake_deps.form_source.latest_for_offer(client_id, args["offer_ref"])
    approved = form.approve(
        approved_by=args.get("approved_by", "hr-officer-eval-approver"),
        approved_at=datetime.now(timezone.utc),
    )
    bundle.intake_deps.form_source.save_for_offer(client_id, args["offer_ref"], approved)
    return None


def _act_inbound_email(bundle: Bundle, viewer: Optional[Viewer], args: dict, setup: dict):
    raw = args.get("raw")
    if raw is None:
        from_addr = args.get("from", setup.get("candidate_email", "eval.candidate@example.test"))
        subject = args["subject"]
        raw = f"From: {from_addr}\nSubject: {subject}\n\nSynthetic acceptance body.\n"
    return handle_acceptance_email(raw, bundle.intake_deps)


def _act_form_approved(bundle: Bundle, viewer: Optional[Viewer], args: dict, setup: dict):
    merged = {**setup, **args}
    client_id = merged.get("client_id", "client-eval-001")
    return handle_form_approved(client_id, args["offer_ref"], bundle.intake_deps)


def _act_compare(bundle: Bundle, viewer: Viewer, args: dict, setup: dict):
    snapshots = [SourceSnapshot(source=s["source"], values=s["values"]) for s in args["snapshots"]]
    return bundle.service.compare_sources(viewer, args["case_id"], snapshots)


def _act_resolve(bundle: Bundle, viewer: Viewer, args: dict, setup: dict):
    return bundle.service.resolve_discrepancy(viewer, args["discrepancy_id"], DiscrepancyDecision(args["decision"]))


def _act_propose_change(bundle: Bundle, viewer: Viewer, args: dict, setup: dict):
    request = FormChangeRequest(
        case_id=args["case_id"],
        requested_by=args["requested_by"],
        change_reason=args["change_reason"],
        candidate_name=args.get("candidate_name"),
        designation=args.get("designation"),
        joining_date=date.fromisoformat(args["joining_date"]) if args.get("joining_date") else None,
        work_location_type=WorkLocationType(args["work_location_type"]) if args.get("work_location_type") else None,
        client_site_id=args.get("client_site_id"),
        salary_and_terms=args.get("salary_and_terms"),
        source_discrepancy_id=args.get("source_discrepancy_id"),
    )
    return bundle.service.propose_form_change(viewer, request, args["approver_id"])


def _act_decide_change(bundle: Bundle, viewer: Viewer, args: dict, setup: dict):
    return bundle.service.decide_form_change(viewer, args["approval_id"], args["approve"], args.get("note", ""))


def _act_decide_variant(bundle: Bundle, viewer: Viewer, args: dict, setup: dict):
    return bundle.service.decide_variant(viewer, args["approval_id"], args["approve"], args.get("note", ""))


def _act_prepare(bundle: Bundle, viewer: Viewer, args: dict, setup: dict):
    return bundle.service.prepare_hris(viewer, args["case_id"])


def _act_decide_write(bundle: Bundle, viewer: Viewer, args: dict, setup: dict):
    return bundle.service.decide_hris_write(viewer, args["approval_id"], args["approve"], args.get("note", ""))


def _act_execute_write(bundle: Bundle, viewer: Viewer, args: dict, setup: dict):
    return bundle.service.execute_hris_write(viewer, args["case_id"])


def _act_set_kill_switch(bundle: Bundle, viewer: Optional[Viewer], args: dict, setup: dict):
    bundle.kill_switch.value = args["value"]
    return None


def _act_fail_gateway_next(bundle: Bundle, viewer: Optional[Viewer], args: dict, setup: dict):
    bundle.change_deps.hris_gateway.fail_next(args.get("n", 1))
    return None


def _act_status(bundle: Bundle, viewer: Viewer, args: dict, setup: dict):
    return bundle.service.get_status(viewer, args["case_id"])


def _act_audit_trail(bundle: Bundle, viewer: Viewer, args: dict, setup: dict):
    return bundle.service.get_audit_trail(viewer, args["case_id"])


ACTIONS = {
    "open_case": _act_open_case,
    "seed_person_record": _act_seed_person_record,
    "seed_offer": _act_seed_offer,
    "seed_form_for_offer": _act_seed_form_for_offer,
    "approve_offer_form": _act_approve_offer_form,
    "inbound_email": _act_inbound_email,
    "form_approved": _act_form_approved,
    "compare": _act_compare,
    "resolve": _act_resolve,
    "propose_change": _act_propose_change,
    "decide_change": _act_decide_change,
    "decide_variant": _act_decide_variant,
    "prepare": _act_prepare,
    "decide_write": _act_decide_write,
    "execute_write": _act_execute_write,
    "set_kill_switch": _act_set_kill_switch,
    "fail_gateway_next": _act_fail_gateway_next,
    "status": _act_status,
    "audit_trail": _act_audit_trail,
}


def _check_expect(result: Any, expect: dict) -> Optional[str]:
    for key, expected in expect.items():
        if key in ("error", "error_type", "error_code"):
            continue
        actual = getattr(result, key, None)
        if hasattr(actual, "value"):
            actual = actual.value
        if actual != expected:
            return f"expected {key}={expected!r}, got {actual!r}"
    return None


def run_scenario(scenario: dict) -> dict:
    setup = scenario.get("setup", {})
    bundle = _build_bundle(setup)
    memory: dict[str, Any] = {}
    failures: list[str] = []

    for i, step in enumerate(scenario["steps"]):
        action = step["action"]
        as_role = step.get("as", "SYSTEM")
        viewer = None if as_role == "SYSTEM" else bundle.viewers.get(as_role)
        args = _resolve(step.get("args", {}), memory)
        expect = step.get("expect", {})
        handler = ACTIONS.get(action)
        if handler is None:
            failures.append(f"step {i}: unknown action {action!r}")
            continue

        try:
            result = handler(bundle, viewer, args, setup)
            error = None
        except Exception as exc:  # the runner must catch anything to report it
            result = None
            error = exc

        if expect.get("error"):
            if error is None:
                failures.append(f"step {i} ({action}): expected an error, none raised")
            else:
                if "error_type" in expect and type(error).__name__ != expect["error_type"]:
                    failures.append(
                        f"step {i} ({action}): expected error_type {expect['error_type']}, "
                        f"got {type(error).__name__}"
                    )
                if "error_code" in expect:
                    code = getattr(error, "code", None)
                    code_value = code.value if hasattr(code, "value") else code
                    if code_value != expect["error_code"]:
                        failures.append(
                            f"step {i} ({action}): expected error_code {expect['error_code']}, got {code_value}"
                        )
        else:
            if error is not None:
                failures.append(f"step {i} ({action}): unexpected error: {type(error).__name__}: {error}")
            else:
                save_key = step.get("save")
                if save_key:
                    memory[save_key] = result
                mismatch = _check_expect(result, expect)
                if mismatch:
                    failures.append(f"step {i} ({action}): {mismatch}")

    final = scenario.get("final", {})
    case_ref = final.get("case_ref", "case")
    case_obj = memory.get(case_ref)
    case_id = None
    if case_obj is not None:
        case_id = getattr(case_obj, "case_id", None)
        if case_id is None and getattr(case_obj, "case", None) is not None:
            case_id = case_obj.case.case_id

    if "stage" in final and case_id:
        try:
            status = bundle.service.get_status(bundle.viewers["AUDITOR"], case_id)
            if status.stage.value != final["stage"]:
                failures.append(f"final: expected stage {final['stage']}, got {status.stage.value}")
        except Exception as exc:
            failures.append(f"final: status check raised {type(exc).__name__}: {exc}")

    if "events" in final:
        for event_name, expected_count in final["events"].items():
            actual = len([e for e in bundle.change_deps.publisher.events if type(e).__name__ == event_name])
            if actual != expected_count:
                failures.append(f"final: expected {expected_count} {event_name}, got {actual}")

    if "gateway_writes" in final:
        actual = bundle.change_deps.hris_gateway.written_count()
        if actual != final["gateway_writes"]:
            failures.append(f"final: expected gateway_writes={final['gateway_writes']}, got {actual}")

    if "audit_actions_present" in final:
        present_actions = {e.action for e in bundle.change_deps.audit_log.entries()}
        for required in final["audit_actions_present"]:
            if required not in present_actions:
                failures.append(f"final: missing audit action {required!r}")

    if final.get("invariants_clean") and case_id:
        violations = check_case_invariants(case_id, bundle.change_deps, policy=bundle.change_deps.policy)
        if violations:
            failures.append(f"final: invariants not clean: {[v.code.value for v in violations]}")

    sensitive_texts = {
        setup.get("salary_and_terms"),
        setup.get("candidate_email"),
        setup.get("candidate_name"),
        *setup.get("extra_sensitive_texts", []),
    }
    sensitive_texts.discard(None)
    sensitive_texts.discard("")
    if sensitive_texts:
        for entry in bundle.change_deps.audit_log.entries():
            dumped = str(entry.model_dump())
            for text in sensitive_texts:
                if text in dumped:
                    failures.append(f"PII leak in audit entry {entry.log_id} (action={entry.action})")
        for event in bundle.change_deps.publisher.events:
            dumped = str(event.model_dump())
            for text in sensitive_texts:
                if text in dumped:
                    failures.append(f"PII leak in event {type(event).__name__}")

        known_case_ids: set[str] = set()
        for value in memory.values():
            cid = getattr(value, "case_id", None)
            if cid is None and getattr(value, "case", None) is not None:
                cid = value.case.case_id
            if cid:
                known_case_ids.add(cid)
        if case_id:
            known_case_ids.add(case_id)

        for known_case_id in known_case_ids:
            for approval in bundle.change_deps.approval_repo.list_for_case(known_case_id):
                dumped = str(approval.model_dump())
                for text in sensitive_texts:
                    if text in dumped:
                        failures.append(f"PII leak in approval {approval.approval_id}")

    return {
        "id": scenario["id"],
        "title": scenario.get("title", ""),
        "covers": scenario.get("covers", []),
        "passed": not failures,
        "failures": failures,
    }


def main() -> int:
    scenarios_dir = Path(__file__).parent / "scenarios"
    scenario_files = sorted(scenarios_dir.glob("*.json"))

    results = []
    all_covers: set[str] = set()
    for path in scenario_files:
        scenario = json.loads(path.read_text())
        result = run_scenario(scenario)
        results.append(result)
        all_covers.update(scenario.get("covers", []))

    output = {
        "results": results,
        "coverage": sorted(all_covers),
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
    }

    out_path = Path(__file__).parent / "results.json"
    out_path.write_text(json.dumps(output, indent=2))

    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"[{status}] {r['id']}: {r['title']}")
        for f in r["failures"]:
            print(f"    - {f}")
    print(f"\n{output['passed']}/{output['total']} scenarios passed")

    return 0 if output["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
