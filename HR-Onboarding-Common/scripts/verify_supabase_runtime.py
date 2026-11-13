"""Live check: UC03, UC04, UC05 and the joining-day jobs on the configured Supabase project, through the code paths the
orchestrator uses (UC03 in process; the real UC04 and UC05 workers; the orchestrator's job store).

DEVELOPMENT DATABASE ONLY. Every write needs --confirm-write <the Supabase host>. It runs on an EXISTING
onboarding_cases row (UC01 is the only writer of that table, and its Supabase writes are blocked on decision B1), and
writes only the rows of the use case each step exercises:

  preflight --case ID                     read-only: the case row, its tenant's configuration rows, the tables
  uc03      --case ID --confirm-write H   Storage object + candidate_documents (upload path), the document collection and
                                          its revision (uc03_case_state "collection:<id>"), UC03's case state, audit
                                          events and document_validations (validation); then a NEW process reloads all
  uc04      --case ID --confirm-write H   the UC04 worker in UC04_STATE_MODE=supabase (startup checks) and UC04's state
                                          backend: one versioned snapshot in audit_events, read back by a new process
  uc05      --case ID --confirm-write H   the UC05 worker in UC05_DATABASE_MODE=supabase: preflight (employee from
                                          onboarding_cases), the offer-accepted event (orientation_sessions,
                                          session_participants, approval_requests, audit_events, agent_runs), snapshot
  jobs      --case ID --confirm-write H   a joining-day job claimed and completed in joining_day_jobs, read back
  negative  --case ID --other-client C    another tenant: refused by UC03's collection, UC05's worker and the job store
  verify    --case ID                     read-only read-back of every row the steps wrote
Run from HR-Onboarding-Common with its .venv. Ids and statuses are printed; never a key, a name or an e-mail address.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import httpx

from hr_onboarding_common.config import load_environment
from hr_onboarding_common.orchestration.adapters import uc03_documents
from hr_onboarding_common.orchestration.contracts import Actor
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.documents import (DocumentIntakeService, SupabaseCandidateDocuments,
                                                          SupabaseObjectStorage, uc03_document_types)
from hr_onboarding_common.orchestration.joining_day_scheduler import (JobStoreUnavailable,
                                                                      SupabaseJoiningDayJobStore)
from hr_onboarding_common.orchestration.state import new_state
from hr_onboarding_common.orchestration.workers.config import WorkerConfig
from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker

ROOT = Path(__file__).resolve().parents[2]
UC03, UC04, UC05 = (ROOT / "UC03_Document_Validation", ROOT / "UC04_Equipment_Access_Integration_Ready",
                    ROOT / "UC05_Orientation-Schedule-Agent")
ACTOR = {"on_behalf_of": "user:hr-admin-verify", "roles": ["hr_admin"]}


# ------------------------------------------------------------------ settings and read access
def settings() -> tuple[str, str]:
    load_environment()
    url = (os.environ.get("ORCHESTRATOR_SUPABASE_URL") or "").strip()
    key = (os.environ.get("ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        sys.exit("ORCHESTRATOR_SUPABASE_URL / ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY are not set (HR-Onboarding-Common/.env)")
    return url, key


def host() -> str:
    return urlparse(settings()[0]).hostname or ""


def require_write(confirm: str | None) -> None:
    if confirm != host():
        sys.exit(f"refusing to write: pass --confirm-write {host()} (the Supabase host this run writes to)")


def get(table: str, **params: str) -> list[dict[str, Any]]:
    url, key = settings()
    response = httpx.get(f"{url.rstrip('/')}/rest/v1/{table}", params=params, timeout=30,
                         headers={"apikey": key, "Authorization": f"Bearer {key}"})
    response.raise_for_status()
    return response.json()


def case_row(case_id: str) -> dict[str, Any]:
    rows = get("onboarding_cases", select="case_id,client_id,employee_no,joining_date,status,department,manager_id,role_id",
               case_id=f"eq.{case_id}")
    if not rows:
        sys.exit(f"no onboarding_cases row {case_id}")
    return rows[0]


def show(title: str, value: Any) -> None:
    print(f"\n== {title}\n{json.dumps(value, indent=1, default=str)}")


# ------------------------------------------------------------------ steps
def preflight(args) -> int:
    case = case_row(args.case)
    show("case", {k: case[k] for k in ("case_id", "client_id", "employee_no", "joining_date", "status")} |
         {"has_department": bool(case.get("department")), "has_manager": bool(case.get("manager_id"))})
    client = case["client_id"]
    show("tenant configuration", {
        "client_configs": len(get("client_configs", select="client_id", client_id=f"eq.{client}")),
        "approved_versions": get("client_config_versions", select="version,status", client_id=f"eq.{client}"),
        "uc04_section": get("client_service_policies", select="config_version,is_active", client_id=f"eq.{client}",
                            service_key="eq.uc04_equipment_access"),
        "buddy_profiles": len(get("buddy_profiles", select="buddy_id", client_id=f"eq.{client}")),
    })
    return 0


def _uc03_deps():
    from integrations.supabase.tables import DuplicateKey, StorageError  # type: ignore

    from hr_onboarding_common.orchestration.uc03_supabase import Uc03PostgrestTables

    url, key = settings()
    return OrchestratorDeps(ctx=None, worker_pool=None, uc03_repo_root=UC03, uc03_store_mode="supabase",
                            uc03_tables_factory=lambda: Uc03PostgrestTables(
                                url=url, service_role_key=key, duplicate_error=DuplicateKey, storage_error=StorageError))


def _pdf(text: str) -> bytes:
    return f"%PDF-1.4\n% {text}\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n".encode()


def uc03(args) -> int:
    require_write(args.confirm_write)
    uc03_documents._ensure_uc03_importable(UC03)
    from src.sub_agent.collection import DocumentCollectionRequest, RequiredDocumentSpec  # type: ignore

    case = case_row(args.case)
    client, case_id = case["client_id"], case["case_id"]
    # 1. The upload path: Storage + candidate_documents, strict (Supabase-authoritative), UC03's type mapping.
    url, key = settings()
    storage = SupabaseObjectStorage(url=url, service_role_key=key, bucket=os.environ.get("DOCUMENT_STORAGE_BUCKET") or "Doc_Collection")
    repo = SupabaseCandidateDocuments(url=url, service_role_key=key, document_types=uc03_document_types(UC03))
    submitted = []

    class Agent:
        def require_role(self, actor, k):
            return None

        def get_case(self, cid, *, client_id):
            return {"case_id": cid, "client_id": client_id,
                    "document_collection": {"plan_version": 1, "required_documents": [{"document_type": "passport"}]}}

        def submit_documents(self, cid, *, documents, actor):
            submitted.extend(documents)
            return {"case_id": cid}

    async def extract(path, document_type):
        return SimpleNamespace(extracted_name=None, confidence=0.9, injection_suspected=False, extracted_fields={}), "verify"

    service = DocumentIntakeService(storage=storage, repository=repo, extractor=extract, strict=True)
    _, receipt = service.upload(Agent(), case_id=case_id, document_type="passport", data=_pdf(f"verify {case_id}"),
                                content_type="application/pdf", file_name="passport.pdf",
                                actor={**ACTOR, "client_id": client})
    show("upload", {k: receipt.view()[k] for k in ("document_id", "storage_status", "metadata_persisted",
                                                    "metadata_reason", "storage_path")})
    # 2. The document collection and its revision, in uc03_case_state.
    deps = _uc03_deps()
    collection = uc03_documents.collection_service(deps)
    collection.open_collection(DocumentCollectionRequest(
        case_id=case_id, tenant_id=client, employee_id=case["employee_no"] or "unknown", plan_id=f"PLAN-{case_id[:8]}",
        plan_version=1, required_documents=[RequiredDocumentSpec(document_type="passport", label="Passport")]))
    receipt2 = collection.submit(case_id=case_id, tenant_id=client, document_type="passport",
                                 document_ref=receipt.document_ref, submitted_by=ACTOR["on_behalf_of"],
                                 metadata=submitted[0]["metadata"])
    show("collection", receipt2.model_dump(mode="json"))
    # 3. Validation through the orchestrator's UC03 adapter: UC03's case state, audit, document_validations.
    state = new_state(case_id=case_id, client_id=client, correlation_id=f"verify-{uuid.uuid4().hex[:8]}", actor=ACTOR,
                      intake_trigger={"candidate_name": "Verified Candidate", "documents": {}},
                      uc02_role_context={"role_id": case.get("role_id") or "", "grade": "", "nationality": "", "location": ""})
    state["employee_id"] = case["employee_no"] or "unknown"
    state["document_collection"] = {"plan_version": 1, "required_documents": [{"document_type": "passport", "required": True}]}
    state["submitted_documents"] = {"passport": {"document_type": "passport", "document_ref": receipt.document_ref,
                                                 "revision": receipt2.revision, "metadata": submitted[0]["metadata"]}}
    result = uc03_documents.call_uc03_validate_documents(state, deps)
    show("validation", {"stage_status": result["stage_statuses"]["validate_documents"],
                        "passport": {k: result["document_validation_result"]["passport"].get(k)
                                     for k in ("classification", "document_status", "persistence_status")}})
    # 4. A NEW process reloads the collection and UC03's case from Supabase.
    out = subprocess.run([sys.executable, __file__, "uc03-reload", "--case", case_id], capture_output=True, text=True,
                         timeout=120)
    print(out.stdout[-2000:] or out.stderr[-2000:])
    return 0


def uc03_reload(args) -> int:
    import asyncio

    uc03_documents._ensure_uc03_importable(UC03)
    deps = _uc03_deps()
    collection = uc03_documents.collection_service(deps)
    case = asyncio.run(deps._uc03_case_store.get_case(args.case))
    show("reloaded in a new process", {
        "collection_plan_version": (collection.collection(args.case) or SimpleNamespace(plan_version=None)).plan_version,
        "passport_revisions": [d.revision for d in collection.history(args.case, "passport")],
        "uc03_case_client": case.client_id if case else None,
        "uc03_received_revisions": dict(case.received_revisions) if case else None})
    return 0


def _worker(uc_id: str, root: Path, **scoped: str) -> UcWorker:
    saved = {k: os.environ.get(k) for k in scoped}
    os.environ.update(scoped)
    try:
        python = root / ".venv" / "Scripts" / "python.exe"
        worker = UcWorker(WorkerConfig(uc_id=uc_id, python_executable=python if python.exists() else Path(sys.executable),
                                       src_dir=root / "src"))
        worker.start()
        return worker
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k)
            else:
                os.environ[k] = v


def uc04(args) -> int:
    require_write(args.confirm_write)
    case = case_row(args.case)
    url, key = settings()
    scoped = {"UC04_WORKER_ENV__UC04_STATE_MODE": "supabase", "UC04_WORKER_ENV__UC04_IDENTITY_PROFILE": "PLATFORM",
              "UC04_WORKER_ENV__SUPABASE_URL": url, "UC04_WORKER_ENV__SUPABASE_SERVICE_ROLE_KEY": key}
    worker = _worker("uc04", UC04, **scoped)  # startup checks: PLATFORM, settings (tables on the first call)
    worker.stop()
    print("uc04 worker started in UC04_STATE_MODE=supabase")
    code = r"""
import json, sys, os
sys.path[:0] = [os.path.join(sys.argv[1], 'src'), sys.argv[1]]
from guardrails import identity; identity.configure('PLATFORM')
import importlib.util
spec = importlib.util.spec_from_file_location('pc', os.path.join(sys.argv[1], 'integrations', 'supabase', 'postgrest_table_client.py'))
pc = importlib.util.module_from_spec(spec); spec.loader.exec_module(pc)
from sub_agent.persistence import AuditEventStateBackend, DatabaseCaseContext
from sub_agent.store import CaseRecord
client = pc.PostgrestTableClient(os.environ['V_URL'], os.environ['V_KEY'])
parent = DatabaseCaseContext(client).get_case(sys.argv[2])
backend = AuditEventStateBackend(client)
stored = backend.load(sys.argv[2])
version = stored.version if stored else 0
if sys.argv[3] == 'write':
    record = stored.record if stored else CaseRecord(sys.argv[2], parent.employee_ref, parent.client_id)
    record.fingerprint = {'verified_at': sys.argv[4]}
    backend.save(record, expected_version=version, new_version=version + 1)
again = backend.load(sys.argv[2])
print(json.dumps({'parent_case': {'employee_ref': parent.employee_ref, 'client_id': parent.client_id, 'status': str(parent.status)},
                  'version_before': version, 'version_after': again.version if again else None,
                  'fingerprint': again.record.fingerprint if again else None}))
"""
    python = UC04 / ".venv" / "Scripts" / "python.exe"
    env = {**os.environ, "V_URL": url, "V_KEY": key}
    stamp = datetime.now(timezone.utc).isoformat()
    for mode in ("write", "read"):  # the read runs in a NEW process: the snapshot is loaded from Supabase
        out = subprocess.run([str(python), "-c", code, str(UC04), case["case_id"], mode, stamp], capture_output=True,
                             text=True, timeout=120, env=env)
        print(f"uc04 state backend ({mode}):", out.stdout.strip() or out.stderr.strip()[-1500:])
    return 0


def _uc05_worker():
    url, key = settings()
    return _worker("uc05", UC05, UC05_WORKER_ENV__UC05_DATABASE_MODE="supabase", UC05_WORKER_ENV__SUPABASE_URL=url,
                   UC05_WORKER_ENV__SUPABASE_SERVICE_ROLE_KEY=key, UC05_OFFER_EVENT_SOURCE="", UC05_BUDDY_SOURCE="",
                   UC05_WELCOME_CONFIG_SOURCE="")


def uc05(args) -> int:
    require_write(args.confirm_write)
    case = case_row(args.case)
    worker = _uc05_worker()
    actor = Actor(on_behalf_of=ACTOR["on_behalf_of"], roles=ACTOR["roles"], client_id=case["client_id"])
    try:
        pre = worker.call("preflight_offer_event", {"event_id": case["case_id"]}, actor=actor, run_id="verify-uc05",
                          idempotency_key=f"verify-uc05-pre-{case['case_id']}")
        show("preflight (employee from onboarding_cases)", pre.result if pre.ok else {"error": pre.error_code,
                                                                                     "detail": pre.error_detail})
        if pre.ok and (pre.result or {}).get("status") == "ok":
            run = worker.call("run_event", {"event": {"event": "offer_accepted", "event_id": case["case_id"]},
                                            "instance": case["case_id"]}, actor=actor, run_id="verify-uc05",
                              idempotency_key=f"verify-uc05-run-{case['case_id']}")
            show("offer_accepted", {"ok": run.ok, "error": run.error_code, "detail": (run.error_detail or "")[:300],
                                    "workflow": (run.result or {}).get("workflow")})
        snap = worker.call("case_snapshot", {"employee_id": case["employee_no"]}, actor=actor, run_id="verify-uc05",
                           idempotency_key="verify-uc05-snap")
        result = snap.result or {}
        show("snapshot", {"ok": snap.ok, "error": snap.error_code, "found": result.get("found"),
                          "case_status": result.get("case_status"), "stage": result.get("stage"),
                          "sessions": [{k: s.get(k) for k in ("session_id", "session_type", "status", "scheduled_start",
                                                               "meeting_link", "room_location", "stored_participants")}
                                       for s in result.get("sessions") or []],
                          "approvals": [{k: a.get(k) for k in ("approval_id", "gate", "status")}
                                        for a in result.get("approvals") or []]})
    finally:
        worker.stop()
    return 0


def jobs(args) -> int:
    require_write(args.confirm_write)
    case = case_row(args.case)
    store = SupabaseJoiningDayJobStore.from_env(os.environ)
    now = datetime.now(timezone.utc)
    run_date = str(case["joining_date"])
    job = store.claim(case["case_id"], run_date, client_id=case["client_id"], now=now, lease=timedelta(minutes=5),
                      max_attempts=5)
    if job is not None:
        store.complete(case["case_id"], run_date, now=now, outcome="verified")
    again = SupabaseJoiningDayJobStore.from_env(os.environ).get(case["case_id"], run_date)  # a new store instance
    show("joining-day job", {"claimed": job is not None, "status": again.status if again else None,
                             "attempts": again.attempts if again else None, "outcome": again.outcome if again else None,
                             "second_claim": store.claim(case["case_id"], run_date, client_id=case["client_id"], now=now,
                                                         lease=timedelta(minutes=5), max_attempts=5)})
    return 0


def negative(args) -> int:
    case = case_row(args.case)
    other = args.other_client
    results = {}
    uc03_documents._ensure_uc03_importable(UC03)
    from src.sub_agent.collection import CollectionError  # type: ignore

    try:
        uc03_documents.collection_service(_uc03_deps()).submit(
            case_id=case["case_id"], tenant_id=other, document_type="passport", document_ref="storage:x/y",
            submitted_by="user:x")
        results["uc03_collection_other_tenant"] = "ACCEPTED (unexpected)"
    except CollectionError as exc:
        results["uc03_collection_other_tenant"] = f"refused: {exc.code}"
    try:
        SupabaseJoiningDayJobStore.from_env(os.environ).claim(case["case_id"], str(case["joining_date"]), client_id=other,
                                                              now=datetime.now(timezone.utc), lease=timedelta(minutes=5),
                                                              max_attempts=5)
        results["jobs_other_tenant"] = "ACCEPTED (unexpected)"
    except JobStoreUnavailable as exc:
        results["jobs_other_tenant"] = f"refused: {exc}"
    worker = _uc05_worker()
    try:
        snap = worker.call("case_snapshot", {"employee_id": case["employee_no"]},
                           actor=Actor(on_behalf_of="user:x", roles=["hr_admin"], client_id=other), run_id="neg",
                           idempotency_key="neg")
        results["uc05_snapshot_other_tenant"] = ("refused: " + str(snap.error_code)) if not snap.ok else (
            "not found" if not (snap.result or {}).get("found") else "FOUND (unexpected)")
    finally:
        worker.stop()
    show("negative (another tenant)", results)
    return 0


def verify(args) -> int:
    case = case_row(args.case)
    cid = case["case_id"]
    show("rows", {
        "candidate_documents": get("candidate_documents", select="document_id,document_type,status", case_id=f"eq.{cid}"),
        "document_validations": get("document_validations", select="validation_id,compliance_item_status,"
                                    "uc03_compliance_status,hr_review_status,document_status", case_id=f"eq.{cid}"),
        "uc03_case_state": get("uc03_case_state", select="case_ref,client_id,status,version",
                               onboarding_case_id=f"eq.{cid}"),
        "uc04_snapshots": get("audit_events", select="event_id,detail", case_id=f"eq.{cid}", action="eq.UC04_CASE_STATE"),
        "orientation_sessions": get("orientation_sessions", select="session_id,session_type,status,start_utc,end_utc,"
                                    "meeting_link,room_location", case_id=f"eq.{cid}"),
        "buddy_assignments": get("buddy_assignments", select="assignment_id,status", case_id=f"eq.{cid}"),
        "communications": get("communications", select="comm_id,status", case_id=f"eq.{cid}"),
        "approval_requests": get("approval_requests", select="request_id,workflow_module,gate,outcome",
                                 case_id=f"eq.{cid}"),
        "agent_runs": get("agent_runs", select="run_id,agent_id,status", case_id=f"eq.{cid}"),
        "joining_day_jobs": get("joining_day_jobs", select="job_id,status,executed_cases_count,run_date",
                                job_id=f"eq.{SupabaseJoiningDayJobStore.job_id(cid, str(case['joining_date']))}"),
        "audit_events_by_action": sorted({r["action"] for r in get("audit_events", select="action", case_id=f"eq.{cid}")}),
    })
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("step", choices=["preflight", "uc03", "uc03-reload", "uc04", "uc05", "jobs", "negative", "verify"])
    parser.add_argument("--case", required=True)
    parser.add_argument("--other-client", default="CLIENT_CANADA")
    parser.add_argument("--confirm-write")
    args = parser.parse_args(argv)
    return {"preflight": preflight, "uc03": uc03, "uc03-reload": uc03_reload, "uc04": uc04, "uc05": uc05, "jobs": jobs,
            "negative": negative, "verify": verify}[args.step](args)


if __name__ == "__main__":
    raise SystemExit(main())
