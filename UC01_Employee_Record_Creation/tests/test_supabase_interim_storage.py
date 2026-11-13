"""The interim Supabase storage process (UC01_SUPABASE_INTERIM_WRITES=1), end to
end against an in-process FAKE PostgREST that mimics the live tables.

This proves the code path - mapping, ordering, client attribution, hash
chain, read-back, no deletes - NOT real Supabase persistence. The real run
needs the team encryption key (B1); see docs/supabase-dev-setup.md.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from sub_agent.adapters.supabase import (
    AuditClientUnknown,
    PostgrestClient,
    SupabaseAuditLog,
    SupabaseCaseRepository,
    SupabaseLogicalDocumentStore,
)
from sub_agent.api.container import build_container
from sub_agent.api.settings import Settings
from sub_agent.audit_integrity import verify_chain
from sub_agent.audit_scope import audit_client_scope
from sub_agent.crypto import generate_key_b64
from sub_agent.documents.providers import MockExtractionProvider
from sub_agent.documents.service import DocumentIntakeContext, DocumentIntakeService, DocumentIntakeStatus
from sub_agent.models import ActorType, AuditEntry
from test_supabase_adapters import BASE, FakePostgrest

FIXTURE = Path(__file__).resolve().parent.parent / "data" / "mock" / "extracted_employee_data.json"
CLIENT = "CLIENT_DUBAI"  # exists live in client_configs with an employee-number counter


class LiveLikeFake(FakePostgrest):
    """FakePostgrest plus what the live tables do that the base fake does not:
    JSON-path filters (detail->>origin), column defaults, rpc/next_employee_no,
    and a record of every DELETE (there must be none)."""

    DEFAULTS = {"onboarding_cases": {"is_deleted": False}}

    def __init__(self) -> None:
        super().__init__()
        self.unique.update({"onboarding_forms": ("form_id",), "onboarding_cases": ("case_id",),
                            "offer_forms": ("client_id", "offer_ref"), "hris_sync_records": ("idempotency_key",)})
        self.counter = 0
        self.deletes: list[str] = []
        self._lock = threading.Lock()

    @staticmethod
    def _matches(row, params):
        plain = {}
        for key, raw in params.items():
            if "->>" in key:
                column, _, field = key.partition("->>")
                op, _, wanted = raw.partition(".")
                assert op == "eq"
                if str((row.get(column) or {}).get(field)) != wanted:
                    return False
            else:
                plain[key] = raw
        return FakePostgrest._matches(row, plain)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            self.deletes.append(request.url.path)
            return httpx.Response(405)
        if "/rpc/next_employee_no" in request.url.path:
            with self._lock:
                self.counter += 1
                return httpx.Response(200, json=f"DXB-{self.counter:06d}")
        response = super().handler(request)
        for table, defaults in self.DEFAULTS.items():
            for row in self.tables.get(table, []):
                for column, value in defaults.items():
                    row.setdefault(column, value)
        return response


def _container(fake, **settings_over):
    values = dict(app_env="dev", supabase_url=BASE, supabase_service_role_key=SecretStr("k"),
                  field_encryption_key=SecretStr(generate_key_b64()), supabase_interim_writes=True,
                  dedup_hmac_key="test", system_api_key="test")
    values.update(settings_over)
    settings = Settings(**values)
    client = PostgrestClient(BASE, SecretStr("k"), transport=httpx.MockTransport(fake.handler))
    return build_container(settings, postgrest=client)


def _context(**over):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))["intake"]
    approval = data["form_approval"]
    base = dict(request_id="REQ-INTERIM-1", client_id=CLIENT, hr_officer_id=data["hr_officer_id"],
                candidate_email=data["candidate_email"], offer_accepted=True,
                work_location_type=data["work_location_type"], placement=data["placement"],
                form_status=approval["form_status"], approved_by=approval["approved_by"],
                approved_at=approval["approved_at"])
    base.update(over)
    return DocumentIntakeContext(**base)


def _store(fake):
    container = _container(fake)
    service = DocumentIntakeService(container.onboarding_deps)
    result = service.process_extraction(MockExtractionProvider(FIXTURE).extract(), _context(), source="mock")
    return container, result


# ------------------------------------------------------------------ the whole process


def test_the_employee_is_stored_across_every_table_and_reads_back():
    fake = LiveLikeFake()
    container, result = _store(fake)
    assert result.status == DocumentIntakeStatus.CREATED, result.errors
    assert result.employee_no == "DXB-000001"  # from rpc/next_employee_no, not Python
    deps = container.onboarding_deps

    case = deps.case_repo.get(result.case_id)  # read back through the Supabase adapter
    assert case.client_id == CLIENT and case.candidate_name == "Maya Synthetic"
    assert case.status.value == "PLAN_PENDING" and case.folder_ref.startswith("logical://uc01/")
    form = deps.form_repo.latest_approved(result.case_id)
    assert form.designation == "Synthetic Data Engineer"
    assert form.salary_and_terms.get_secret_value().startswith("SYNTHETIC")  # decrypted with the key
    raw_form = fake.tables["onboarding_forms"][0]
    assert "17,000" not in json.dumps(raw_form)  # encrypted at rest
    assert fake.tables["person_records"][0]["case_id"] == result.case_id
    assert fake.tables["domain_events_outbox"]  # CaseOpenedEvent persisted


def test_every_audit_row_has_a_real_client_and_the_chain_verifies():
    fake = LiveLikeFake()
    container, result = _store(fake)
    rows = fake.tables["audit_events"]
    assert rows and {r["client_id"] for r in rows} == {CLIENT}
    assert all(r["detail"]["origin"] == "uc01" for r in rows)
    assert [r["sequence"] for r in rows] == list(range(len(rows)))
    entries = container.onboarding_deps.audit_log.entries()
    assert verify_chain(entries) == []  # the existing in-memory chain rules hold
    actions = {e.action for e in entries}
    assert {"START_GATE_EVALUATED", "CASE_OPENED", "DOCUMENT_EXTRACTED"} <= actions


def test_nothing_is_ever_deleted():
    fake = LiveLikeFake()
    _store(fake)
    assert fake.deletes == []


def test_a_repeat_submission_links_the_existing_case_and_stores_no_second_employee():
    fake = LiveLikeFake()
    container, first = _store(fake)
    service = DocumentIntakeService(container.onboarding_deps)
    again = service.process_extraction(MockExtractionProvider(FIXTURE).extract(),
                                       _context(request_id="REQ-INTERIM-2"), source="mock")
    assert again.status == DocumentIntakeStatus.LINKED_EXISTING and again.existing_case_id == first.case_id
    assert len(fake.tables["onboarding_cases"]) == 1 and fake.counter == 1


# ------------------------------------------------------------------ client attribution


def _audit_log(fake, case_client=lambda case_id: None):
    client = PostgrestClient(BASE, SecretStr("k"), transport=httpx.MockTransport(fake.handler))
    return SupabaseAuditLog(client, case_client=case_client)


def _entry(**over):
    base = dict(request_id="r1", actor_type=ActorType.SYSTEM, actor_id="uc01-test", action="TEST", reference="r1")
    base.update(over)
    return AuditEntry(**base)


def test_an_entry_without_any_resolvable_client_is_refused_not_invented():
    fake = LiveLikeFake()
    with pytest.raises(AuditClientUnknown):
        _audit_log(fake).append(_entry())
    assert fake.tables.get("audit_events", []) == []


def test_client_resolution_order_entry_then_case_then_scope():
    fake = LiveLikeFake()
    log = _audit_log(fake, case_client=lambda case_id: "FROM-CASE")
    log.append(_entry(client_id="FROM-ENTRY", case_id="c1"))
    log.append(_entry(case_id="c1"))
    with audit_client_scope("FROM-SCOPE"):
        log.append(_entry())
    assert [r["client_id"] for r in fake.tables["audit_events"]] == ["FROM-ENTRY", "FROM-CASE", "FROM-SCOPE"]


def test_the_chain_ignores_other_use_cases_rows():
    fake = LiveLikeFake()
    fake.tables["audit_events"] = [{"event_id": "other", "client_id": "X", "detail": {"source": "uc05"},
                                    "sequence": None, "entry_hash": None}]
    log = _audit_log(fake)
    with audit_client_scope(CLIENT):
        log.append(_entry())
        log.append(_entry())
    ours = [r for r in fake.tables["audit_events"] if (r.get("detail") or {}).get("origin") == "uc01"]
    assert [r["sequence"] for r in ours] == [0, 1]
    assert verify_chain(log.entries()) == []


def test_sensitive_values_are_still_refused_before_any_write():
    from sub_agent.audit_integrity import SensitiveValueInAuditError

    fake = LiveLikeFake()
    with pytest.raises(SensitiveValueInAuditError):
        with audit_client_scope(CLIENT):
            _audit_log(fake).append(_entry(after={"salary_and_terms": "x"}))
    assert fake.tables.get("audit_events", []) == []


# ------------------------------------------------------------------ gating


def test_interim_writes_are_refused_outside_dev():
    with pytest.raises(RuntimeError, match="development-only"):
        _container(LiveLikeFake(), app_env="production")


def test_without_the_flag_the_container_still_refuses():
    from sub_agent.adapters.supabase.unsupported import UnsupportedAuditLog, UnsupportedCaseRepository

    fake = LiveLikeFake()
    settings = Settings(app_env="dev", supabase_url=BASE, supabase_service_role_key=SecretStr("k"),
                        field_encryption_key=SecretStr(generate_key_b64()))
    deps = build_container(settings, postgrest=PostgrestClient(
        BASE, SecretStr("k"), transport=httpx.MockTransport(fake.handler))).onboarding_deps
    assert isinstance(deps.case_repo, UnsupportedCaseRepository) and isinstance(deps.audit_log, UnsupportedAuditLog)


def test_the_flag_wires_the_interim_adapters_but_not_offers():
    from sub_agent.adapters.supabase.unsupported import UnsupportedOfferRepository

    container = _container(LiveLikeFake())
    deps = container.onboarding_deps
    assert isinstance(deps.case_repo, SupabaseCaseRepository) and isinstance(deps.audit_log, SupabaseAuditLog)
    assert isinstance(deps.document_store, SupabaseLogicalDocumentStore)
    assert isinstance(container.intake_deps.offer_repo, UnsupportedOfferRepository)  # B3


def test_settings_read_the_flag_from_the_environment():
    assert Settings.from_env({"UC01_SUPABASE_INTERIM_WRITES": "1"}).supabase_interim_writes is True
    assert Settings.from_env({}).supabase_interim_writes is False


def test_logical_folder_references_are_validated():
    import uuid

    from sub_agent.adapters.supabase.storage_paths import UnsafeObjectPath

    store = SupabaseLogicalDocumentStore()
    case_id = str(uuid.uuid4())
    assert store.create_case_folder(CLIENT, case_id) == f"logical://uc01/{CLIENT}/{case_id}"
    with pytest.raises(UnsafeObjectPath):
        store.create_case_folder("../x", case_id)
    assert re.match(r"^logical://uc01/", store.create_case_folder(CLIENT, case_id))
