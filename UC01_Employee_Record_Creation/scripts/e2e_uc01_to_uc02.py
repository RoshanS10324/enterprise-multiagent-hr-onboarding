"""UC-01 -> persistence -> read-back -> handoff -> shared orchestrator -> UC-02.

    python scripts/e2e_uc01_to_uc02.py run --persistence memory   --out RUN_DIR [--source mock|docx] [--twice]
    python scripts/e2e_uc01_to_uc02.py run --persistence supabase --out RUN_DIR
    python scripts/e2e_uc01_to_uc02.py verify-restart --out RUN_DIR      # in a NEW process

Every step reports one label:

    PASS          verified against the actually configured system
    MOCK PASS     executed for real, but storage was in-memory
    BLOCKED       a decision, configuration or implementation prevents it
    NOT VERIFIED  not executed
    FAIL          executed and failed

What is real: UC-01's DocumentIntakeService.process_extraction (validation,
HR review, then the unchanged open_onboarding_case: start gate, duplicates,
placement, employee number, approvals, audit); the shared orchestrator's
OnboardingOrchestratorAgent and its plan_tasks stage adapter
call_uc02_plan_tasks; UC-02 unchanged.

--source mock (default) reads data/mock/sample_employee_record.json (client
CLIENT_DUBAI) through MockExtractionProvider - it replaces ONLY the
document-reading step.

STRICT DATABASE-FIRST RULE: UC-02 is triggered only when the employee was
saved through UC-01's Supabase adapters AND read back from them and matched.
Otherwise the run stops with "STOP - DO NOT TRIGGER UC-02". There is no
fallback to an in-memory employee.

--persistence supabase uses the configured Supabase project for UC-01 (needs
the team UC01_FIELD_ENCRYPTION_KEY and UC01_SUPABASE_INTERIM_WRITES=1 in dev)
and UC-02's own Supabase mode for this process. It checks readiness FIRST and
stops with BLOCKED - before UC-01 writes anything and without triggering
UC-02 - while any required UC-01 repository refuses or the configuration is
incomplete (decisions B1-B9).

--persistence memory runs UC-01 on its in-memory repositories (MOCK PASS) and,
by the rule above, always stops before UC-02.

The full orchestrator graph is not run from intake: its UC-01 worker path is
blocked by the undecided location -> location_name mapping, and plan_tasks
only follows UC-03 document verification. The intake stage result is recorded
from the UC-01 run, exactly as adapters/uc01_intake.py records CASE_OPENED.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

UC01 = Path(__file__).resolve().parent.parent
REPO = UC01.parent
sys.path.insert(0, str(UC01 / "src"))

FIXTURE = UC01 / "data" / "mock" / "sample_employee_record.json"
AS_OF = date(2026, 11, 4)
PASS, MOCK_PASS, BLOCKED, NOT_VERIFIED, FAIL = "PASS", "MOCK PASS", "BLOCKED", "NOT VERIFIED", "FAIL"
STEPS = (
    "1 load mock extracted data", "2 extraction provider", "3 UC-01 validation and business rules",
    "4 UC-01 employee creation", "5 persist employee", "6 read employee back", "7 build and validate UC-02 input",
    "8 trigger shared orchestrator (plan_tasks)", "9 UC-02 onboarding workflow", "10 persist UC-02 results",
    "11 read back employee and tasks", "12 report",
)
def supabase_backed(container) -> bool:
    """True only when UC-01's case and audit stores are the live Supabase adapters.

    The UC-02 trigger rule is computed from this, never from a flag: an
    employee held in memory can never reach UC-02.
    """
    from sub_agent.adapters.supabase import SupabaseAuditLog, SupabaseCaseRepository

    deps = container.onboarding_deps
    return isinstance(deps.case_repo, SupabaseCaseRepository) and isinstance(deps.audit_log, SupabaseAuditLog)


#: Read-back fields that must equal the intake data before UC-02 is triggered.
_COMPARED = ("candidate_name", "candidate_email", "designation", "joining_date", "location_name", "client_id")


class Report:
    def __init__(self, persistence: str, source: str) -> None:
        self.data: dict[str, Any] = {"persistence": persistence, "source": source,
                                     "steps": {s: {"status": NOT_VERIFIED} for s in STEPS}}

    def step(self, number: str, status: str, **detail: Any) -> None:
        key = next(s for s in STEPS if s.split(" ", 1)[0] == number)
        self.data["steps"][key] = {"status": status, **detail}

    def stop(self, reason: str) -> dict[str, Any]:
        self.data["stopped"] = reason
        return self.data


# ------------------------------------------------------------------- UC-01


def uc01_memory_container():
    os.environ["SUPABASE_URL"] = ""  # a preset empty value wins over .env: memory, whatever .env says
    from sub_agent.api.container import build_container
    from sub_agent.api.settings import Settings

    return build_container(Settings(app_env="dev", dedup_hmac_key="e2e-only", system_api_key="e2e-only"))


def uc01_supabase_container(client_id: Optional[str] = None) -> tuple[Optional[Any], list[str]]:
    """The container for the configured Supabase project, and every blocker.

    No network: building the container makes no call (the ping runs only when
    the API server starts). Blockers name variables and adapters, never values.
    """
    from pydantic import ValidationError

    try:
        # Importing sub_agent.api builds UC-01's default app from .env, so an
        # incomplete configuration fails here already - report it, don't crash.
        from sub_agent.api.container import build_container
        from sub_agent.api.settings import Settings

        settings = Settings.from_env()
    except ValidationError as exc:
        return None, ["configuration: " + "; ".join(e["msg"] for e in exc.errors())]
    from sub_agent.adapters.supabase.unsupported import _Refusing

    if not settings.persistence_enabled:
        return None, ["configuration: SUPABASE_URL is not set"]
    try:
        container = build_container(settings)
    except RuntimeError as exc:  # e.g. interim writes outside APP_ENV=dev
        return None, [f"configuration: {exc}"]
    d = container.onboarding_deps
    # The ports the document -> case path writes to. Offers (B3) are used only
    # by the email-intake path, so they do not block this one.
    refusing = [type(r).__name__ for r in (d.case_repo, d.audit_log, d.approval_repo, d.number_generator,
                                           d.document_store) if isinstance(r, _Refusing)]
    blockers = [f"repository not enabled for Supabase: {name} (set UC01_SUPABASE_INTERIM_WRITES=1 in dev, "
                "or wait for the DBA decisions)" for name in refusing]
    if not blockers and client_id and container.postgrest is not None:
        # Read-only: every case needs an existing client (FK onboarding_cases.client_id).
        found = container.postgrest.select("client_configs", columns="client_id",
                                           filters={"client_id": f"eq.{client_id}"}, limit=1)
        if not found:
            blockers.append(f"client {client_id!r} is not in client_configs; the database accepts employees "
                            "only for an existing client")
    return container, blockers


def intake_context(fixture: dict[str, Any]):
    from sub_agent.documents.service import DocumentIntakeContext

    intake, approval = fixture["intake"], fixture["intake"]["form_approval"]
    return DocumentIntakeContext(
        request_id=f"E2E-{intake['offer_reference']}", client_id=intake["client_id"],
        hr_officer_id=intake["hr_officer_id"], candidate_email=intake["candidate_email"],
        offer_accepted=intake["offer_accepted"], work_location_type=intake["work_location_type"],
        placement=intake.get("placement"), form_status=approval["form_status"],
        approved_by=approval.get("approved_by"), approved_at=approval.get("approved_at"),
    )


def read_back(container, case_id: str) -> dict[str, Any]:
    deps = container.onboarding_deps
    case = deps.case_repo.get(case_id)
    if case is None:
        raise LookupError(f"case {case_id} not found on read-back")
    form = deps.form_repo.latest_approved(case_id)
    if form is None:
        raise LookupError(f"no approved form for case {case_id} on read-back")
    return {
        "case_id": case.case_id, "client_id": case.client_id, "employee_no": case.employee_no,
        "candidate_name": case.candidate_name, "candidate_email": case.candidate_email,
        "joining_date": case.joining_date.isoformat(), "work_location_type": case.work_location_type.value,
        "case_status": case.status.value, "location_name": case.location_name, "department": case.department,
        "grade": case.grade, "nationality_group": case.nationality_group, "project_name": case.project_name,
        "form_id": form.form_id, "form_status": form.status.value, "designation": form.designation,
        "form_approved_at": form.approved_at.isoformat() if form.approved_at else None,
        "salary_decrypts": bool(form.salary_and_terms.get_secret_value()),
        "approvals": len(deps.approval_repo.list_for_case(case_id)),
        "audit_entries_for_case": len(deps.audit_log.entries_for_case(case_id)),
    }


def expected_from_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    e, i = fixture["extracted"]["employee"], fixture["intake"]
    return {"candidate_name": e["employee_name"], "candidate_email": i["candidate_email"],
            "designation": e["designation"], "joining_date": e["joining_date"],
            "location_name": i["placement"]["location_name"], "client_id": i["client_id"]}


# ------------------------------------------------------------------- handoff + orchestrator


#: What the shared adapter (orchestration/adapters/uc02_tasks.py) refuses to plan without.
UC02_REQUIRED_FIELDS = ("role_id", "department", "manager_id", "location", "project")


class HandoffInputRequired(ValueError):
    """A field the UC-02 contract requires has no source (or two sources disagree).

    HR must supply it. Nothing is defaulted or invented, and UC-02 is not called.
    """

    def __init__(self, missing: list[str], conflicts: dict[str, dict[str, str]] | None = None) -> None:
        self.missing, self.conflicts = list(missing), dict(conflicts or {})
        parts = [f"missing {self.missing}"] if self.missing else []
        parts += [f"conflicting {name}: {srcs}" for name, srcs in self.conflicts.items()]
        super().__init__("HR input required for the UC-02 handoff: " + "; ".join(parts))


def _given(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def uc02_fields(rb: dict[str, Any], intake: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """The UC-02 role-context fields and where each came from, or HandoffInputRequired.

    project: the employee record's persisted ``project_name`` (placement data
    the intake caller gave UC-01) or the caller's ``uc02.project`` -- the two
    sources OnboardingCaseInput.project is documented to come from. If both
    are given they must agree. Neither the form, UC-02's configuration nor a
    default is used: the onboarding form has no project field.
    """
    caller = intake.get("uc02") or {}
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    missing: list[str] = []
    conflicts: dict[str, dict[str, str]] = {}
    for name in ("role_id", "manager_id", "location"):
        if (v := _given(caller.get(name))) is not None:
            values[name], sources[name] = v, f"intake.uc02.{name}"
        else:
            missing.append(name)
    if (dept := _given(rb.get("department"))) is not None:
        values["department"], sources["department"] = dept, "uc01_case.department"
    else:
        missing.append("department")
    stored, supplied = _given(rb.get("project_name")), _given(caller.get("project"))
    if stored and supplied and stored != supplied:
        conflicts["project"] = {"uc01_case.project_name": stored, "intake.uc02.project": supplied}
    elif stored or supplied:
        values["project"] = stored or supplied  # type: ignore[assignment]
        sources["project"] = "uc01_case.project_name" if stored else "intake.uc02.project"
    else:
        missing.append("project")
    if missing or conflicts:
        raise HandoffInputRequired(sorted(missing, key=UC02_REQUIRED_FIELDS.index), conflicts)
    return values, sources


def handoff(rb: dict[str, Any], fixture: dict[str, Any]):
    """OnboardingCaseInput built from the READ-BACK, plus caller-supplied UC-02 fields.

    Raises HandoffInputRequired before anything reaches UC-02 when a required
    field has no source."""
    from hr_onboarding_common.orchestration.contracts import OnboardingCaseInput

    intake = fixture["intake"]
    fields, _ = uc02_fields(rb, intake)
    payload = {
        "client_id": rb["client_id"], "hr_officer_id": intake["hr_officer_id"], "offer_accepted": True,
        "offer_reference": intake["offer_reference"], "candidate_name": rb["candidate_name"],
        "candidate_email": rb["candidate_email"], "designation": rb["designation"],
        "joining_date": rb["joining_date"], "work_location_type": rb["work_location_type"],
        "employee_id": rb["employee_no"], "department": rb["department"],
        "grade": rb["grade"], "nationality_group": rb["nationality_group"],
        "offer_accepted_at": rb["form_approved_at"], "form_approved_at": rb["form_approved_at"],
        "form_approved_by_role": intake["form_approval"]["approved_by_role"],
        # The contract's "UC02 fields UC01 does not carry": the intake caller's values, and the
        # project from the employee record or the caller (uc02_fields, never defaulted).
        **{k: fields[k] for k in ("role_id", "manager_id", "location", "project")},
        # Opt-in checklist attributes (visa route, travel, insurance...) exactly as the caller states
        # them; UC-02 never infers them, and none is derived here.
        "attributes": intake["uc02"].get("attributes"),
    }
    return OnboardingCaseInput.model_validate({k: v for k, v in payload.items() if v is not None})


def orchestrator():
    from hr_onboarding_common.orchestration.bootstrap import build_deps
    from hr_onboarding_common.orchestration.orchestrator_agent import OnboardingOrchestratorAgent

    ctx, deps = build_deps(agent_home=REPO / "HR-Onboarding-Common",
                           uc03_repo_root=REPO / "UC03_Document_Validation", ucs=())
    return OnboardingOrchestratorAgent(ctx), deps


def plan_tasks(agent, deps, inp, rb: dict[str, Any]) -> dict[str, Any]:
    from hr_onboarding_common.orchestration.adapters._shared import with_stage_status
    from hr_onboarding_common.orchestration.adapters.uc02_tasks import call_uc02_plan_tasks
    from hr_onboarding_common.orchestration.state import Stage, StageStatus

    state = agent.initial_state(inp)
    state["uc02_role_context"]["as_of"] = AS_OF.isoformat()
    state["case_id"], state["employee_id"], state["joining_date"] = rb["case_id"], rb["employee_no"], rb["joining_date"]
    state = with_stage_status(state, Stage.INTAKE, StageStatus.COMPLETE)
    result = call_uc02_plan_tasks(state, deps)
    return {"plan_tasks_status": result["stage_statuses"][Stage.PLAN_TASKS.value],
            "error_code": result.get("error_code"), "error_detail": result.get("error_detail"),
            "case_id": result["case_id"], "employee_id": result.get("employee_id"),
            "task_plan": result.get("task_plan")}


def uc02_actor(client_id: str, actor_id: str):
    from onboarding_task_management.contracts import Actor, Role

    return Actor(actor_id=actor_id, role=Role.HR_OPS, client_id=client_id)


def describe_uc02(service, client_id: str, onboarding_id: str) -> dict[str, Any]:
    actor = uc02_actor(client_id, "SYN-HR-OFFICER-01")
    view = service.checklist(actor, onboarding_id, AS_OF)
    risk = service.joining_risk(actor, onboarding_id, AS_OF)
    return {
        "onboarding_id": onboarding_id, "client_id": view.client_id, "employee_id": view.employee_id,
        "joining_date": view.joining_date.isoformat(), "revision": view.revision,
        "tasks": [{"task_id": e.task_id, "template_key": e.template_key, "title": e.title,
                   "category": e.category.value, "owner": e.owner.value, "status": e.status.value,
                   "due_date": e.due_date.isoformat(), "depends_on": e.depends_on}
                  for e in view.entries],
        "excluded_templates": [d.template_key for d in view.excluded],
        "milestones": [{"template_key": m.template_key, "status": m.status.value,
                        "target_date": m.target_date.isoformat()}
                       for m in service.milestones(actor, onboarding_id, AS_OF)],
        "risk": {"at_risk": risk.at_risk, "days_to_joining": risk.days_to_joining, "reasons": risk.reasons},
    }


def uc02_configuration(client_id: str, joining_date: str) -> dict[str, Any]:
    """The configuration UC-02 itself would use for this client, read through UC-02's own registry.

    Same client id as the saved employee, the version in force on the joining
    date. Raises (never falls back to another client) when it is missing,
    draft, invalid or incomplete.
    """
    from datetime import date as _date

    from onboarding_task_management.sub_agent.runtime import build_service

    cfg = build_service().registry.get(client_id, on=_date.fromisoformat(joining_date))
    if cfg.client_id != client_id:
        raise LookupError(f"UC-02 returned the configuration of {cfg.client_id!r} for {client_id!r}")
    return {"client_id": cfg.client_id, "version": cfg.version, "approval_status": cfg.approval_status,
            "data_origin": getattr(cfg, "data_origin", ""), "template_count": len(cfg.templates),
            "template_keys": sorted(t.key for t in cfg.templates),
            "milestone_count": len(cfg.milestones)}


def plan_in_database(postgrest, case_id: str) -> dict[str, Any]:
    """onboarding_tasks / task_dependencies rows for the case, read straight from the database."""
    tasks = postgrest.select("onboarding_tasks", columns=("task_id,case_id,client_id,employee_id,template_key,title,"
                                                         "owner_role,assigned_actor_id,status,due_date,priority,"
                                                         "config_version"),
                             filters={"case_id": f"eq.{case_id}"})
    ids = [t["task_id"] for t in tasks]
    deps = postgrest.select("task_dependencies", columns="task_id,depends_on_task_id,client_id,dependency_type",
                            filters={"task_id": f"in.({','.join(ids)})"}) if ids else []
    return {"tasks": tasks, "dependencies": deps}


# ------------------------------------------------------------------- pipeline


def run_pipeline(*, fixture_path: Path = FIXTURE, persistence: str = "memory", source: str = "mock",
                 docx: Optional[Path] = None, container=None, agent=None, deps=None,
                 client_id: Optional[str] = None, storage_label: Optional[str] = None) -> dict[str, Any]:
    from sub_agent.documents.providers import DocxExtractionProvider, MockExtractionProvider, PdfExtractionProvider
    from sub_agent.documents.service import DocumentIntakeService

    rep = Report(persistence, source)
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    if client_id:  # store under another (existing) client; the comparison follows it
        fixture["intake"]["client_id"] = client_id
    rep.step("1", PASS, fixture=Path(fixture_path).name, synthetic=fixture.get("synthetic") is True)

    if source == "mock":
        provider: Any = MockExtractionProvider(fixture_path)
    else:  # an uploaded document: .pdf or .docx, read by the real readers
        cls = PdfExtractionProvider if Path(docx).suffix.lower() == ".pdf" else DocxExtractionProvider
        provider = cls(Path(docx).read_bytes(), Path(docx).name)
    extraction = provider.extract()
    md = extraction.metadata
    rep.step("2", PASS, provider=type(provider).__name__, template_recognised=md.template_recognised,
             completeness=md.completeness, missing_fields=md.missing_fields, invalid_fields=md.invalid_fields,
             uncertain_fields=md.uncertain_fields)

    if container is None:
        if persistence == "supabase":
            container, blockers = uc01_supabase_container(fixture["intake"]["client_id"])
            if blockers:
                rep.step("5", BLOCKED, blockers=blockers,
                         note="checked before UC-01 ran: nothing was written to Supabase")
                return rep.stop("Supabase persistence is not ready; UC-02 was NOT triggered")
        else:
            container = uc01_memory_container()
    storage = storage_label or (PASS if persistence == "supabase" else MOCK_PASS)
    storage_name = ("Supabase" if persistence == "supabase" and storage == PASS
                    else "UC-01 in-memory or fake repositories (NOT Supabase)")

    service = DocumentIntakeService(container.onboarding_deps, llm=container.onboarding_deps.llm,
                                    today=lambda: AS_OF)
    if source == "mock":
        result = service.process_extraction(extraction, intake_context(fixture), source=provider.source)
    else:  # the upload path itself: read -> extract -> validate -> open_onboarding_case
        result = service.process(Path(docx).read_bytes(), Path(docx).name, intake_context(fixture))
    status = result.status.value
    if status in ("VALIDATION_FAILED", "NEEDS_REVIEW", "HELD", "PAUSED", "REJECTED"):
        rep.step("3", FAIL if status == "VALIDATION_FAILED" else BLOCKED, uc01_status=status,
                 errors=result.errors, review_reasons=result.review_reasons, reasons=result.reasons)
        return rep.stop(f"UC-01 returned {status}; no employee created, UC-02 NOT triggered")
    rep.step("3", PASS, uc01_status=status, review_reasons=result.review_reasons)
    if status == "PERSISTENCE_FAILED":
        rep.step("4", FAIL, uc01_status=status, errors=result.errors)
        rep.step("5", FAIL, storage=storage_name, errors=result.errors)
        return rep.stop("employee persistence failed; UC-02 NOT triggered")
    created = status == "CREATED"
    case_id = result.case_id if created else result.existing_case_id
    rep.step("4", PASS, uc01_status=status, case_id=case_id, employee_no=result.employee_no,
             approvals_created=result.approvals_created,
             note=None if created else "duplicate request linked to the existing case; nothing new created")
    rep.step("5", storage, storage=storage_name, source_document_stored=result.source_document_stored)

    try:
        rb = read_back(container, case_id)
    except Exception as exc:  # noqa: BLE001 - any read-back failure stops the handoff
        rep.step("6", FAIL, storage=storage_name, error=f"{type(exc).__name__}: {exc}")
        return rep.stop("employee read-back failed; UC-02 NOT triggered")
    expected = expected_from_fixture(fixture)
    mismatches = sorted(k for k in _COMPARED if rb[k] != expected[k])
    if mismatches:
        rep.step("6", FAIL, storage=storage_name, mismatched_fields=mismatches)
        return rep.stop("read-back does not match the intake data; UC-02 NOT triggered")
    rep.step("6", storage, storage=storage_name, compared_fields=list(_COMPARED), read_back=rb)

    # STRICT DATABASE-FIRST RULE:
    #   employee_saved_to_supabase AND employee_readback_verified -> trigger UC-02
    #   otherwise -> STOP, do not trigger UC-02.
    employee_saved_to_supabase = supabase_backed(container) and status in ("CREATED", "LINKED_EXISTING")
    employee_readback_verified = not mismatches  # step 6 passed: read back from that same store and matched
    rep.data["uc02_gate"] = {"employee_saved_to_supabase": employee_saved_to_supabase,
                             "employee_readback_verified": employee_readback_verified}
    if not (employee_saved_to_supabase and employee_readback_verified):
        return rep.stop("STOP - DO NOT TRIGGER UC-02: the employee is not saved in and verified from Supabase "
                        f"(UC-01 storage was {storage_name})")

    try:
        config = uc02_configuration(rb["client_id"], rb["joining_date"])
    except Exception as exc:  # noqa: BLE001 - missing/draft/invalid configuration stops the handoff
        rep.step("7", BLOCKED, client_id=rb["client_id"], configuration_error=f"{type(exc).__name__}: {exc}")
        return rep.stop(f"UC-02 has no usable approved configuration for {rb['client_id']}; UC-02 NOT triggered "
                        "(no other client's configuration is used)")
    rep.data["uc02_configuration"] = config
    try:
        inp = handoff(rb, fixture)
    except HandoffInputRequired as exc:
        rep.step("7", BLOCKED, error_code="HR_INPUT_REQUIRED", hr_input_required=exc.missing,
                 conflicts=exc.conflicts, detail=str(exc))
        return rep.stop(f"{exc}; UC-02 NOT triggered")
    rep.step("7", PASS, contract="hr_onboarding_common.orchestration.contracts.OnboardingCaseInput",
             configuration={k: config[k] for k in ("client_id", "version", "approval_status", "template_count")},
             source_of_values="UC-01 read-back + intake-caller UC-02 fields",
             payload=inp.model_dump(mode="json", exclude_none=True))

    if agent is None or deps is None:
        agent, deps = orchestrator()
    planned = plan_tasks(agent, deps, inp, rb)
    if planned["plan_tasks_status"] != "complete":
        rep.step("8", FAIL, **{k: planned[k] for k in ("plan_tasks_status", "error_code", "error_detail")})
        try:
            intact = read_back(container, case_id)["case_id"] == case_id
        except Exception:  # noqa: BLE001
            intact = False
        rep.data["employee_intact_after_uc02_failure"] = intact
        return rep.stop("UC-02 did not complete planning; the employee record was left as is")
    rep.step("8", PASS, adapter="orchestration.adapters.uc02_tasks.call_uc02_plan_tasks",
             case_id=planned["case_id"], employee_id=planned["employee_id"],
             uc02_created_new_plan=planned["task_plan"]["created"])

    uc02 = deps._uc02_sub_agent.service
    onboarding_id = planned["task_plan"]["onboarding_id"]
    state = describe_uc02(uc02, rb["client_id"], onboarding_id)
    complete = all(t["title"] and t["template_key"] and t["owner"] and t["due_date"] and t["status"]
                   for t in state["tasks"])
    keys_known = {t["template_key"] for t in state["tasks"]} <= set(config["template_keys"])
    if state["client_id"] != rb["client_id"] or not state["tasks"] or not complete or not keys_known:
        rep.step("9", FAIL, **state, same_client=state["client_id"] == rb["client_id"],
                 every_task_complete=complete, templates_from_this_configuration=keys_known)
        return rep.stop("UC-02's checklist is not a complete plan from this client's configuration")
    rep.step("9", PASS, **state, same_client=True, configuration_version=config["version"])
    uc02_store = os.environ.get("UC02_STORE_MODE", "memory")
    if uc02_store == "supabase" and getattr(container, "postgrest", None) is not None and storage == PASS:
        db = plan_in_database(container.postgrest, rb["case_id"])
        by_id = {t["task_id"]: t for t in db["tasks"]}
        expected_edges = {(t["task_id"], d) for t in state["tasks"] for d in t["depends_on"]}
        stored_edges = {(d["task_id"], d["depends_on_task_id"]) for d in db["dependencies"]}
        checks = {
            "every_task_stored": set(by_id) == {t["task_id"] for t in state["tasks"]},
            "same_client": all(t["client_id"] == rb["client_id"] for t in db["tasks"]),
            "same_employee": all(t["employee_id"] == rb["employee_no"] for t in db["tasks"]),
            "configuration_version": sorted({str(t["config_version"]) for t in db["tasks"]}) == [str(config["version"])],
            "dependencies_stored": expected_edges <= stored_edges,
            "priority_and_owner_set": all(t["priority"] and t["owner_role"] for t in db["tasks"]),
        }
        rep.step("10", PASS if all(checks.values()) else FAIL, uc02_store=uc02_store, checks=checks,
                 task_rows=len(db["tasks"]), dependency_rows=len(db["dependencies"]),
                 rows=[{k: t[k] for k in ("template_key", "title", "owner_role", "assigned_actor_id", "status",
                                          "due_date", "priority", "config_version")} for t in db["tasks"]])
    else:
        rep.step("10", MOCK_PASS, uc02_store=uc02_store,
                 note="UC-02 local store, not the Supabase database" if uc02_store != "supabase" else
                      "UC-01 storage was not the live database")

    if uc02_store in ("sqlite", "supabase"):
        from onboarding_task_management.sub_agent.runtime import build_service

        again = describe_uc02(build_service(), rb["client_id"], onboarding_id)  # a NEW service over the store
        same = again["tasks"] == state["tasks"] and again["milestones"] == state["milestones"]
        rep.step("11", (PASS if storage == PASS and uc02_store == "supabase" else MOCK_PASS) if same else FAIL,
                 employee_read_back=storage_name, uc02_tasks_read_back=f"fresh UC-02 service over {uc02_store}",
                 tasks_match=same, task_count=len(again["tasks"]))
    rep.data["identifiers"] = {"client_id": rb["client_id"], "case_id": rb["case_id"],
                               "employee_no": rb["employee_no"], "uc02_onboarding_id": onboarding_id,
                               "uc02_employee_id": state["employee_id"]}
    rep.step("12", PASS)
    return rep.data


def verify_restart(run_dir: Path) -> dict[str, Any]:
    from onboarding_task_management.sub_agent.runtime import build_service

    saved = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    if "identifiers" not in saved:
        return {"status": NOT_VERIFIED, "reason": saved.get("stopped", "the run did not reach UC-02")}
    ids = saved["identifiers"]
    if saved["persistence"] == "supabase":
        container, blockers = uc01_supabase_container(ids["client_id"])
        if blockers:
            return {"status": BLOCKED, "blockers": blockers}
        case = container.onboarding_deps.case_repo.get(ids["case_id"])  # a NEW process reading Supabase
        uc01 = {"case_found_after_restart": case is not None,
                "same_employee_no": case is not None and case.employee_no == ids["employee_no"],
                "status": PASS if case is not None and case.employee_no == ids["employee_no"] else "FAIL"}
    else:
        uc01 = {"status": NOT_VERIFIED, "note": "not a Supabase run"}
    before = saved["steps"]["9 UC-02 onboarding workflow"]
    now = describe_uc02(build_service(), ids["client_id"], ids["uc02_onboarding_id"])
    return {
        "uc02": {"same_onboarding_and_client": now["onboarding_id"] == ids["uc02_onboarding_id"]
                 and now["client_id"] == ids["client_id"],
                 "same_employee_id": now["employee_id"] == ids["employee_no"],
                 "tasks_identical": now["tasks"] == before["tasks"],
                 "milestones_identical": now["milestones"] == before["milestones"],
                 "task_count": len(now["tasks"]), "storage": os.environ.get("UC02_STORE_MODE")},
        "uc01": uc01,
    }


def verify_update(run_dir: Path) -> dict[str, Any]:
    """UC-01's own form-change flow on the stored employee: propose -> approve -> read back.

    Changes only the synthetic salary text (encrypted) of the run's own case;
    nothing that UC-02's checklist depends on.
    """
    from pydantic import SecretStr

    from sub_agent.form_changes import approve_form_change, propose_form_change
    from sub_agent.models.form_change import FormChangeRequest

    saved = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    if saved.get("persistence") != "supabase" or "uc02_gate" not in saved:
        return {"status": NOT_VERIFIED, "reason": "only for a Supabase run that saved its employee"}
    ids = saved.get("identifiers") or {"case_id": saved["steps"]["4 UC-01 employee creation"]["case_id"],
                                       "client_id": saved["steps"]["6 read employee back"]["read_back"]["client_id"]}
    container, blockers = uc01_supabase_container(ids["client_id"])
    if blockers:
        return {"status": BLOCKED, "blockers": blockers}
    deps = container.deps
    before = deps.form_repo.latest_approved(ids["case_id"])
    new_terms = "SYNTHETIC: AED 18,500 per month (verify-update)"
    proposed = propose_form_change(FormChangeRequest(case_id=ids["case_id"], requested_by="SYN-HR-OFFICER-21",
                                                     change_reason="SYNTHETIC verify-update",
                                                     salary_and_terms=SecretStr(new_terms)),
                                   deps, approver_id="SYN-HR-MANAGER-21")
    approve_form_change(proposed.approval_id, "SYN-HR-MANAGER-21", deps)
    fresh, _ = uc01_supabase_container(ids["client_id"])  # read back through a new container
    after = fresh.deps.form_repo.latest_approved(ids["case_id"])
    approval = fresh.deps.approval_repo.get(proposed.approval_id)
    raw = fresh.postgrest.select("onboarding_forms", columns="form_id,version,status,salary_and_terms_encrypted",
                                 filters={"case_id": f"eq.{ids['case_id']}"})
    from sub_agent.adapters.supabase.repositories import decode_bytea
    from sub_agent.crypto import FieldCipher

    current = next(r for r in raw if r["form_id"] == after.form_id)
    checks = {
        "new_version": after.version == before.version + 1,
        "decrypts_to_the_new_value": after.salary_and_terms.get_secret_value() == new_terms,
        "encrypted_at_rest": "18,500" not in json.dumps(raw),
        "key_id_recorded": FieldCipher.key_id_of(decode_bytea(current["salary_and_terms_encrypted"]))
        == (container.settings.field_encryption_key_id or None),
        "approval_decided": approval is not None and approval.status.value == "APPROVED",
        "previous_version_kept": any(r["form_id"] == before.form_id for r in raw),
    }
    return {"status": PASS if all(checks.values()) else "FAIL", "checks": checks,
            "versions": sorted((r["version"], r["status"]) for r in raw), "approval_id": proposed.approval_id,
            "changed_fields": proposed.changed_fields}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["run", "verify-restart", "verify-update"])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--persistence", choices=["memory", "supabase"], default="memory")
    parser.add_argument("--source", choices=["mock", "docx", "document"], default="mock",
                        help="mock = the fixture's extracted data; document/docx = upload --document (.pdf or .docx)")
    parser.add_argument("--document", "--docx", dest="docx", type=Path,
                        default=UC01 / "data" / "mock" / "documents" / "SYNTHETIC_sample_employee.pdf")
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--twice", action="store_true", help="run again in the same process (duplicate check)")
    parser.add_argument("--client", help="store under this existing client id instead of the fixture's")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.persistence == "supabase":
        # UC-02's own Supabase mode, for THIS process only (UC-02's files and
        # configuration are not touched): its documented development settings.
        from dotenv import dotenv_values

        for name, value in dotenv_values(REPO / "UC02_Onboarding_Task_Agent" / ".env").items():
            if value is not None:
                os.environ.setdefault(name, value)
        os.environ["UC02_STORE_MODE"] = "supabase"
        os.environ.setdefault("UC02_ENV", "development")
        os.environ.setdefault("UC02_ALLOW_SYNTHETIC_CONFIG", "1")
    else:
        os.environ["UC02_STORE_MODE"] = "sqlite"
        os.environ["UC02_SQLITE_PATH"] = str(args.out / "uc02_tasks.sqlite")
    os.environ.setdefault("LLM_MODE", "offline")

    if args.command == "verify-restart":
        result: dict[str, Any] = verify_restart(args.out)
        (args.out / "restart_report.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    elif args.command == "verify-update":
        result = verify_update(args.out)
        (args.out / "update_report.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    else:
        if (args.out / "report.json").exists():
            raise SystemExit(f"refusing to overwrite {args.out / 'report.json'}: use a fresh --out directory")
        kwargs = dict(fixture_path=args.fixture, persistence=args.persistence, source=args.source, docx=args.docx,
                      client_id=args.client)
        if args.twice:
            # Memory: one shared store. Supabase: each run builds its own container
            # and finds the first run's employee in the database.
            container = uc01_memory_container() if args.persistence == "memory" else None
            agent, deps = orchestrator()
            first = run_pipeline(**kwargs, container=container, agent=agent, deps=deps)
            second = run_pipeline(**kwargs, container=container, agent=agent, deps=deps)
            result = {**first, "second_run": second}
        else:
            result = run_pipeline(**kwargs)
        (args.out / "report.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(json.dumps(result, indent=1, default=str))
    return 0 if "stopped" not in result else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
