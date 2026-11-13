"""SupabaseCaseRepository mapping, the APPROVED_SUPABASE_PORTS gate, extra
employee-number cases and document object paths. Fake PostgREST only - none
of this is a real Supabase test."""

from __future__ import annotations

import json
import uuid
from datetime import date

import httpx
import pytest
from pydantic import SecretStr

from sub_agent.adapters.supabase import (
    CaseStatusMappingMissing,
    PostgrestClient,
    SupabaseApprovalRepository,
    SupabaseCaseRepository,
    SupabaseEmployeeNumberGenerator,
)
from sub_agent.adapters.supabase.client import SupabaseError
from sub_agent.adapters.supabase.repositories import VARIANT_COLUMNS
from sub_agent.adapters.supabase.storage_paths import UnsafeObjectPath, case_object_path, client_of
from sub_agent.adapters.supabase.unsupported import (
    UnsupportedApprovalRepository,
    UnsupportedCaseRepository,
    UnsupportedEmployeeNumberGenerator,
)
from sub_agent.api import container as container_module
from sub_agent.api.container import build_container
from sub_agent.api.settings import Settings
from sub_agent.crypto import generate_key_b64
from sub_agent.models import Case, CasePlacement, CaseStatus, VariantFlags, WorkLocationType
from test_supabase_adapters import BASE, FakePostgrest

# A TEST-ONLY mapping. B5 is undecided; nothing in src/ ships a default.
TEST_MAPPING = {status: f"TEST_{status.value}" for status in CaseStatus}


class FakeWithDefaults(FakePostgrest):
    """Applies onboarding_cases.is_deleted's live default (false) on insert."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = super().handler(request)
        for row in self.tables.get("onboarding_cases", []):
            row.setdefault("is_deleted", False)
        return response


def _client(fake) -> PostgrestClient:
    return PostgrestClient(BASE, SecretStr("test-service-key"), transport=httpx.MockTransport(fake.handler))


def _case(**over) -> Case:
    base = dict(client_id="SYN-UC01-DOC-TEST", employee_no="EMP-000042", candidate_name="Case Synthetic",
                candidate_email="case.synthetic@example.invalid", hr_officer_id="SYN-HR-1",
                joining_date=date(2027, 2, 1), work_location_type=WorkLocationType.OFFICE,
                current_form_version=1, variants=VariantFlags(is_rehire=True, is_uae_resident=True),
                **CasePlacement(location_name="SYN Dubai Office").model_dump())
    base.update(over)
    return Case(**base)


@pytest.fixture
def fake():
    db = FakeWithDefaults()
    db.unique["onboarding_cases"] = ("case_id",)
    return db


# ------------------------------------------------------------------ case mapping


def test_case_maps_the_variant_rename_and_round_trips(fake):
    repo = SupabaseCaseRepository(_client(fake), status_mapping=TEST_MAPPING)
    case = _case()
    repo.save(case)
    row = fake.tables["onboarding_cases"][0]
    assert row["variant_is_resident"] is True and "variant_is_uae_resident" not in row
    assert row["variant_is_rehire"] is True and row["variant_is_minor"] is False
    assert set(VARIANT_COLUMNS.values()) <= set(row)
    assert repo.get(case.case_id) == case


def test_placement_nulls_are_explicit_so_live_defaults_never_apply(fake):
    repo = SupabaseCaseRepository(_client(fake), status_mapping=TEST_MAPPING)
    repo.save(_case())
    row = fake.tables["onboarding_cases"][0]
    for column in ("department", "grade", "nationality_group", "project_name"):
        assert column in row and row[column] is None  # not omitted: "General" etc. must not appear
    assert row["onboarding_attributes"] == []  # NOT NULL text[]: always an array
    assert "role_id" not in row and "manager_id" not in row  # D1/D2: not written


def test_onboarding_attributes_are_written_as_given(fake):
    repo = SupabaseCaseRepository(_client(fake), status_mapping=TEST_MAPPING)
    case = _case(onboarding_attributes=["visa_required", "needs_travel"])
    repo.save(case)
    assert fake.tables["onboarding_cases"][0]["onboarding_attributes"] == ["visa_required", "needs_travel"]
    assert repo.get(case.case_id).onboarding_attributes == ["visa_required", "needs_travel"]


def test_save_twice_updates_in_place(fake):
    repo = SupabaseCaseRepository(_client(fake), status_mapping=TEST_MAPPING)
    case = _case()
    repo.save(case)
    repo.save(case.model_copy(update={"status": CaseStatus.ON_HOLD}))
    assert len(fake.tables["onboarding_cases"]) == 1
    assert repo.get(case.case_id).status == CaseStatus.ON_HOLD


def test_soft_deleted_rows_are_not_returned(fake):
    repo = SupabaseCaseRepository(_client(fake), status_mapping=TEST_MAPPING)
    case = _case()
    repo.save(case)
    fake.tables["onboarding_cases"][0]["is_deleted"] = True
    assert repo.get(case.case_id) is None and repo.list_for_client(case.client_id) == []


def test_without_an_approved_status_mapping_nothing_is_written(fake):
    repo = SupabaseCaseRepository(_client(fake))  # B5 undecided: no mapping
    with pytest.raises(CaseStatusMappingMissing, match="B5"):
        repo.save(_case())
    assert fake.tables.get("onboarding_cases", []) == [] and fake.requests == []


def test_an_unmappable_live_status_is_refused_on_read(fake):
    repo = SupabaseCaseRepository(_client(fake), status_mapping=TEST_MAPPING)
    case = _case()
    repo.save(case)
    fake.tables["onboarding_cases"][0]["status"] = "INTAKE_PENDING"  # another use case's vocabulary
    with pytest.raises(CaseStatusMappingMissing):
        repo.get(case.case_id)


def test_a_status_mapping_must_be_one_to_one():
    with pytest.raises(ValueError, match="one-to-one"):
        SupabaseCaseRepository(_client(FakePostgrest()), status_mapping={s: "SAME" for s in CaseStatus})


# ------------------------------------------------------------------ wiring gate


def _supabase_container(monkeypatch, ports):
    monkeypatch.setattr(container_module, "APPROVED_SUPABASE_PORTS", frozenset(ports))
    settings = Settings(app_env="dev", supabase_url=BASE, supabase_service_role_key=SecretStr("k"),
                        field_encryption_key=SecretStr(generate_key_b64()))
    return build_container(settings, postgrest=_client(FakePostgrest()))


def test_no_port_is_approved_so_the_container_still_refuses(monkeypatch):
    assert container_module.APPROVED_SUPABASE_PORTS == frozenset()
    deps = _supabase_container(monkeypatch, set()).onboarding_deps
    assert isinstance(deps.approval_repo, UnsupportedApprovalRepository)
    assert isinstance(deps.number_generator, UnsupportedEmployeeNumberGenerator)
    assert isinstance(deps.case_repo, UnsupportedCaseRepository)


def test_an_approved_port_wires_its_live_adapter(monkeypatch):
    deps = _supabase_container(monkeypatch, {"approvals", "employee_numbers"}).onboarding_deps
    assert isinstance(deps.approval_repo, SupabaseApprovalRepository)
    assert isinstance(deps.number_generator, SupabaseEmployeeNumberGenerator)
    assert isinstance(deps.case_repo, UnsupportedCaseRepository)  # no live case adapter is wired


# ------------------------------------------------------------------ employee numbers: failure modes


def _rpc_reply(status: int, body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)
    return PostgrestClient(BASE, SecretStr("k"), transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status,body", [
    (404, {"code": "PGRST202", "message": "Could not find the function"}),  # function missing
    (403, {"code": "42501", "message": "permission denied for function"}),  # no EXECUTE grant
])
def test_a_missing_function_or_grant_is_a_clear_error(status, body):
    with pytest.raises(SupabaseError):
        SupabaseEmployeeNumberGenerator(_rpc_reply(status, body)).next("SYN-UC01-DOC-TEST")


def test_a_client_without_a_counter_row_is_refused():
    """The reference function returns NULL when the client has no counter."""
    with pytest.raises(SupabaseError, match="unexpected result shape"):
        SupabaseEmployeeNumberGenerator(_rpc_reply(200, None)).next("SYN-UC01-DOC-TEST")


# ------------------------------------------------------------------ document object paths


def test_object_paths_are_built_from_ids_only():
    case_id, doc_id = str(uuid.uuid4()), str(uuid.uuid4())
    path = case_object_path("SYN-UC01-DOC-TEST", case_id, doc_id)
    assert path == f"SYN-UC01-DOC-TEST/{case_id}/{doc_id}.docx"
    assert client_of(path) == "SYN-UC01-DOC-TEST"


@pytest.mark.parametrize("client_id,case_id,doc_id,ext", [
    ("../other", str(uuid.uuid4()), str(uuid.uuid4()), "docx"),
    ("SYN/x", str(uuid.uuid4()), str(uuid.uuid4()), "docx"),
    ("SYN", "../../etc", str(uuid.uuid4()), "docx"),
    ("SYN", str(uuid.uuid4()), "Maya Synthetic offer", "docx"),
    ("SYN", str(uuid.uuid4()), str(uuid.uuid4()), "exe"),
    ("", str(uuid.uuid4()), str(uuid.uuid4()), "docx"),
])
def test_unsafe_object_path_parts_are_refused(client_id, case_id, doc_id, ext):
    with pytest.raises(UnsafeObjectPath):
        case_object_path(client_id, case_id, doc_id, ext)
