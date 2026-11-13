"""SupabaseApprovalRepository (uc01_approvals) and SupabaseEmployeeNumberGenerator
(rpc/next_employee_no), against the in-process fake PostgREST. No network.

These prove the ADAPTERS: column mapping, filters, error handling and that
the adapter itself adds no race. They do NOT prove the live database
function is concurrency-safe - that is decision B7.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import SecretStr

from sub_agent.adapters.memory import InMemoryApprovalRepository
from sub_agent.adapters.supabase import (
    PostgrestClient,
    SupabaseApprovalRepository,
    SupabaseEmployeeNumberGenerator,
)
from sub_agent.adapters.supabase.client import SupabaseError
from sub_agent.api.container import build_container
from sub_agent.models import Approval, ApprovalStatus, Checkpoint
from test_supabase_adapters import BASE, FakePostgrest

CASE_ID = "7b1c2d3e-0000-4000-8000-000000000001"


class FakeWithRpc(FakePostgrest):
    """Adds POST /rpc/next_employee_no with an atomic counter, and a
    configurable reply to test the adapter's shape checks."""

    def __init__(self, reply=None) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.counter: dict[str, int] = {}
        self.reply = reply
        self.rpc_calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "/rpc/" in request.url.path:
            args = json.loads(request.content)
            self.rpc_calls.append(args)
            if self.reply is not None:
                return httpx.Response(200, json=self.reply)
            with self._lock:
                n = self.counter.get(args["p_client_id"], 0) + 1
                self.counter[args["p_client_id"]] = n
            return httpx.Response(200, json=f"EMP-{n:06d}")
        return super().handler(request)


def _client(fake) -> PostgrestClient:
    return PostgrestClient(BASE, SecretStr("test-service-key"), transport=httpx.MockTransport(fake.handler))


def _approval(**over) -> Approval:
    base = dict(case_id=CASE_ID, checkpoint=Checkpoint.HC_12_VARIANT, subject_ref="variant:is_rehire",
                requested_to="hr-officer-1", requested_by=None, reason="rehire proposed")
    base.update(over)
    return Approval(**base)


# ------------------------------------------------------------------ approvals


def test_an_approval_round_trips_every_field():
    fake = FakeWithRpc()
    repo = SupabaseApprovalRepository(_client(fake))
    a = _approval(requested_by="hr-officer-2", decision_note="n")
    repo.add(a)
    assert repo.get(a.approval_id) == a
    stored = fake.tables["uc01_approvals"][0]
    assert set(stored) == {"approval_id", "case_id", "checkpoint", "subject_ref", "requested_to", "created_at",
                           "requested_by", "status", "decided_by", "decided_at", "reason", "decision_note"}
    assert stored["checkpoint"] == "HC_12_VARIANT" and stored["status"] == "PENDING"


def test_update_and_pending_list_mirror_the_in_memory_repository():
    fake = FakeWithRpc()
    live, mem = SupabaseApprovalRepository(_client(fake)), InMemoryApprovalRepository()
    first, second = _approval(), _approval(subject_ref="variant:is_minor", requested_to="hr-officer-9")
    for repo in (live, mem):
        repo.add(first)
        repo.add(second)
        decided = first.model_copy(update={"status": ApprovalStatus.APPROVED, "decided_by": "hr-manager-1",
                                           "decided_at": datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)})
        repo.update(decided)
    for name in ("hr-officer-1", "hr-officer-9"):
        assert [a.approval_id for a in live.list_pending_for(name)] == \
               [a.approval_id for a in mem.list_pending_for(name)]
    assert {a.approval_id for a in live.list_for_case(CASE_ID)} == {first.approval_id, second.approval_id}
    assert live.get(first.approval_id).status == ApprovalStatus.APPROVED


def test_updating_a_missing_approval_raises_like_the_in_memory_repository():
    repo = SupabaseApprovalRepository(_client(FakeWithRpc()))
    with pytest.raises(KeyError):
        repo.update(_approval())
    with pytest.raises(KeyError):
        InMemoryApprovalRepository().update(_approval())


def test_a_missing_approval_reads_as_none():
    assert SupabaseApprovalRepository(_client(FakeWithRpc())).get("00000000-0000-4000-8000-000000000000") is None


# ------------------------------------------------------------------ employee numbers


def test_numbers_are_allocated_by_one_rpc_call_per_number():
    fake = FakeWithRpc()
    gen = SupabaseEmployeeNumberGenerator(_client(fake))
    assert [gen.next("SYN-UC01-DOC-TEST") for _ in range(3)] == ["EMP-000001", "EMP-000002", "EMP-000003"]
    assert fake.rpc_calls == [{"p_client_id": "SYN-UC01-DOC-TEST"}] * 3
    assert gen.next("OTHER-CLIENT") == "EMP-000001"  # per-client counter


def test_the_adapter_adds_no_race_of_its_own():
    fake = FakeWithRpc()
    gen = SupabaseEmployeeNumberGenerator(_client(fake))
    with ThreadPoolExecutor(max_workers=10) as pool:
        numbers = list(pool.map(lambda _: gen.next("SYN-UC01-DOC-TEST"), range(20)))
    assert len(set(numbers)) == 20


@pytest.mark.parametrize("reply", ["EMP-000007", ["EMP-000007"], [{"next_employee_no": "EMP-000007"}]])
def test_accepted_reply_shapes(reply):
    assert SupabaseEmployeeNumberGenerator(_client(FakeWithRpc(reply=reply))).next("c") == "EMP-000007"


@pytest.mark.parametrize("reply", ["", 42, [], ["a", "b"], {"a": 1, "b": 2}, "EMP 000007; DROP", "x" * 40])
def test_an_unexpected_reply_is_refused_not_guessed(reply):
    with pytest.raises(SupabaseError, match="unexpected result shape"):
        SupabaseEmployeeNumberGenerator(_client(FakeWithRpc(reply=reply))).next("c")


def test_a_database_error_surfaces_as_a_supabase_error():
    def refuse(request):
        return httpx.Response(500, json={"code": "XX000", "message": "boom"})

    client = PostgrestClient(BASE, SecretStr("k"), transport=httpx.MockTransport(refuse))
    with pytest.raises(SupabaseError):
        SupabaseEmployeeNumberGenerator(client).next("c")


# ------------------------------------------------------------------ still fail-closed


def test_the_container_still_refuses_case_audit_and_number_writes_on_supabase():
    """Until B1/B2/B6/B7 are decided the Supabase container keeps refusing."""
    from sub_agent.adapters.supabase.unsupported import (
        UnsupportedApprovalRepository,
        UnsupportedAuditLog,
        UnsupportedCaseRepository,
        UnsupportedEmployeeNumberGenerator,
    )
    from sub_agent.api.settings import Settings
    from sub_agent.crypto import generate_key_b64

    settings = Settings(app_env="dev", supabase_url=BASE, supabase_service_role_key=SecretStr("k"),
                        field_encryption_key=SecretStr(generate_key_b64()))
    container = build_container(settings, postgrest=_client(FakeWithRpc()))
    deps = container.onboarding_deps
    assert isinstance(deps.case_repo, UnsupportedCaseRepository)
    assert isinstance(deps.audit_log, UnsupportedAuditLog)
    assert isinstance(deps.approval_repo, UnsupportedApprovalRepository)
    assert isinstance(deps.number_generator, UnsupportedEmployeeNumberGenerator)
