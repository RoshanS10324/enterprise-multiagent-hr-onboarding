"""UC04's client configuration from the central Supabase configuration (orchestration/uc04_central_config.py).

Every database response here is MOCKED: an in-process PostgREST stand-in (FakeSupabase) behind the shared REST
client's transport hook. Its rows are produced by UC02's own seeding projection from the real client profiles in
client_configuration/profiles, so they have exactly the shape the live seeding wrote. Nothing touches a real database.
"""

from __future__ import annotations

import copy
import uuid
from collections import defaultdict
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from _helpers import BASE, UC_ROOTS, require_uc, venv_python
from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.orchestration import joining_date, uc04_central_config as cc
from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import call_uc04_equipment_access
from hr_onboarding_common.orchestration.contracts import WorkerResponse
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state
from hr_onboarding_common.orchestration.tenant_config import TenantConfig

document = pytest.importorskip("onboarding_task_management.client_configuration.document")
projection = pytest.importorskip("onboarding_task_management.client_configuration.projection")
from onboarding_task_management.persistence import PersistenceError  # noqa: E402
from onboarding_task_management.supabase_rest import SupabaseRestClient  # noqa: E402

PROFILES = BASE / "client_configuration" / "profiles"
JOIN = date(2026, 12, 14)
TEST_DATA_OK = {"UC02_ENV": "test", "UC02_ALLOW_SYNTHETIC_CONFIG": "1"}  # the shared policy's explicit opt-in
S = Stage.EQUIPMENT_ACCESS.value


# --------------------------------------------------------------------------- mocked Supabase


class FakeSupabase:
    """GET-only PostgREST stand-in: eq filters and `order=<col>.desc`. `leak=True` ignores the filters, as a
    misconfigured view or policy would; `fail={table: status | "unreachable"}` injects database failures."""

    def __init__(self):
        self.tables: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.requests: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.leak = False
        self.fail: dict[str, Any] = {}

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        table = parts.path.rsplit("/", 1)[-1]
        params = parse_qsl(parts.query)
        self.requests.append((method, table, params))
        assert method == "GET", "the configuration read must never write"
        if table in self.fail:
            if self.fail[table] == "unreachable":
                raise PersistenceError("supabase is unreachable", reason="URLError")
            return self.fail[table], {"message": "refused"}
        rows = [copy.deepcopy(r) for r in self.tables[table]]
        if not self.leak:
            for col, value in params:
                if value.startswith("eq."):
                    rows = [r for r in rows if str(r.get(col)).lower() == value[3:].lower()]
        for col, value in params:
            if col == "order":
                key, _, direction = value.partition(".")
                rows.sort(key=lambda r: int(r[key]) if str(r[key]).isdigit() else r[key], reverse=direction == "desc")
        return 200, rows


def load(name: str) -> dict[str, Any]:
    return document.load_document(PROFILES / name)


INDIA = "existing_mock_clients/client_india_config.json"
CANADA = "existing_mock_clients/client_canada_config.json"
CLIENT_C = "client_c_config.json"  # grade-dependent equipment matrix
CLIENT_D = "client_d_config.json"  # Sunday-Thursday week, country and client-site rules


def seed(fake: FakeSupabase, doc: dict[str, Any], **client_row: Any) -> str:
    """Load one client exactly as the seeding projection writes it. Existing (mock) clients keep their own
    client_configs row live, which the projection does not produce: it is built as the live mock rows are."""
    rows = projection.project(doc)
    cid = doc["client_master"]["client_id"]
    row = (rows["client_configs"][0] if rows["client_configs"] else
           {"client_id": cid, "is_active": True, "config_hash": f"mock-{cid.lower()}-v2",
            "settings_payload": {"environment": "mock"}})
    row.update(client_row)
    fake.tables["client_configs"].append(row)
    fake.tables["client_config_versions"] += [dict(r, version=int(r["version"])) for r in rows["client_config_versions"]]
    fake.tables["client_service_policies"] += [dict(r, config_version=int(r["config_version"]))
                                               for r in rows["client_service_policies"]]
    return cid


def section_rows(fake: FakeSupabase, cid: str) -> dict[str, dict[str, Any]]:
    return {r["service_key"]: r for r in fake.tables["client_service_policies"]
            if r["client_id"] == cid and r["service_category"] == "cfg_section"}


def rehash(fake: FakeSupabase, cid: str) -> None:
    """After editing a stored section, make sections_hash consistent again, so only the edit itself is tested."""
    rows = section_rows(fake, cid)
    new = projection.sections_hash({k: r["policy_details"]["document"] for k, r in rows.items()})
    for r in rows.values():
        r["policy_details"]["sections_hash"] = new


def rest(fake: FakeSupabase) -> SupabaseRestClient:
    return SupabaseRestClient("https://unit-test.invalid", "test-only-key", transport=fake)


def resolve(fake, cid, role, grade=None, environ=TEST_DATA_OK, on=JOIN):
    return cc.resolve(rest(fake), client_id=cid, role=role, grade=grade, joining_date=on, environ=environ)


def sections(doc):
    return {s: doc[s] for s in document.SECTIONS}


# --------------------------------------------------------------------------- mapping (pure)


def test_india_maps_onto_uc04_case_config_exactly():
    doc = load(INDIA)
    uc04 = doc["uc04_equipment_access"]
    config = cc.to_case_config(sections(doc), client_id="CLIENT_INDIA", role="software_engineer", grade=None, version=2)
    assert config["client_id"] == "CLIENT_INDIA" and config["role_id"] == "software_engineer" and config["version"] == "v2"
    expected_eq = next(m["items"] for m in uc04["role_equipment_matrix"] if m["role_id"] == "software_engineer")
    expected_ac = next(m["items"] for m in uc04["role_access_matrix"] if m["role_id"] == "software_engineer")
    assert config["role_equipment_matrix"] == {"state": "VERIFIED", "value": expected_eq}
    assert config["role_access_matrix"] == {"state": "VERIFIED", "value": expected_ac}
    assert config["approver_config"]["value"] == {k: {"role": v["role"], "delegate_role": v["delegate_role"]}
                                                  for k, v in uc04["approver_config"].items()}
    assert config["lead_times"]["value"] == uc04["lead_times"]
    assert config["handover_rules"]["value"] == uc04["handover_rules"]
    # Central evidence keys are lower case with one role; UC04 wants EvidenceType keys with role lists.
    assert config["evidence_roles"]["value"] == {k.upper(): [v] for k, v in uc04["evidence_roles"].items()}
    # India works Monday-Saturday (ISO 1-6): UC04 counts 0 = Monday, so the weekend is Sunday = 6.
    assert config["work_calendar"]["value"]["weekend_days"] == [6]
    observed = sorted(h["date"] for h in doc["shared_configuration"]["calendar"]["holidays"] if h["observed"])
    assert config["work_calendar"]["value"]["holidays"] == observed
    # Null in the central document is "not configured": MISSING for UC04, never invented.
    assert config["country_rules"] == {"state": "MISSING"} and config["client_site_rules"] == {"state": "MISSING"}
    assert "client_overrides" not in config  # UC04's default (no overrides) applies


def test_sunday_to_thursday_week_country_and_client_site_rules():
    doc = load(CLIENT_D)
    config = cc.to_case_config(sections(doc), client_id="SYN-CLIENT-D", role="operations_associate", grade=None,
                               version=1)
    assert config["work_calendar"]["value"]["weekend_days"] == [4, 5]  # Friday and Saturday
    assert config["country_rules"] == {"state": "VERIFIED", "value": doc["uc04_equipment_access"]["country_rules"]}
    assert config["client_site_rules"] == {"state": "VERIFIED",
                                           "value": doc["uc04_equipment_access"]["client_site_rules"]}


def test_canada_country_rules_are_carried():
    doc = load(CANADA)
    config = cc.to_case_config(sections(doc), client_id="CLIENT_CANADA", role="software_engineer", grade=None, version=2)
    assert config["country_rules"]["value"] == {"country_code": "CA", "courier_extra_days": 2,
                                                "remote_first_day_required": True}
    assert config["work_calendar"]["value"]["weekend_days"] == [5, 6]


def test_a_grade_dependent_matrix_needs_the_grade_and_never_guesses():
    s = sections(load(CLIENT_C))
    entry = next(m for m in s["uc04_equipment_access"]["role_equipment_matrix"] if m["role_id"] == "senior_manager")
    ok = cc.to_case_config(s, client_id="SYN-CLIENT-C", role="senior_manager", grade=entry["grades"][0], version=1)
    assert ok["role_equipment_matrix"]["value"] == entry["items"]
    with pytest.raises(cc.Uc04ConfigError) as no_grade:
        cc.to_case_config(s, client_id="SYN-CLIENT-C", role="senior_manager", grade=None, version=1)
    assert no_grade.value.code == cc.GRADE_REQUIRED
    with pytest.raises(cc.Uc04ConfigError) as other_grade:
        cc.to_case_config(s, client_id="SYN-CLIENT-C", role="senior_manager", grade="G1", version=1)
    assert other_grade.value.code == cc.ROLE_NOT_CONFIGURED


def test_role_absent_from_both_matrices_is_refused_and_absent_from_one_is_verified_empty():
    s = sections(load(INDIA))
    with pytest.raises(cc.Uc04ConfigError) as unknown:
        cc.to_case_config(s, client_id="CLIENT_INDIA", role="astronaut", grade=None, version=2)
    assert unknown.value.code == cc.ROLE_NOT_CONFIGURED
    uc04 = s["uc04_equipment_access"]
    uc04["role_equipment_matrix"] = [m for m in uc04["role_equipment_matrix"] if m["role_id"] != "software_engineer"]
    config = cc.to_case_config(s, client_id="CLIENT_INDIA", role="software_engineer", grade=None, version=2)
    assert config["role_equipment_matrix"] == {"state": "VERIFIED_EMPTY"}
    assert config["role_access_matrix"]["state"] == "VERIFIED"


@pytest.mark.parametrize("edit, fragment", [
    (lambda u, sh: u["evidence_roles"].update({"teleport": "x"}), "unknown evidence types"),
    (lambda u, sh: next(m for m in u["role_equipment_matrix"] if m["role_id"] == "software_engineer")["items"]
     .append("no-such-item"), "not in the equipment catalogue"),
    (lambda u, sh: u["lead_times"].update({"EQUIPMENT": -1}), "lead_times"),
    (lambda u, sh: u.update(country_rules={"country_code": "IN", "courier_extra_days": 1,
                                           "remote_first_day_required": False, "weekend_days": [7]}), "country_rules"),
    (lambda u, sh: u.update(out_of_matrix_policy="allow"), "out_of_matrix_policy"),
    (lambda u, sh: sh["calendar"].update(weekend_days=[5]), "contradicts"),
    (lambda u, sh: u["approver_config"].pop("EXCEPTION"), "approver_config"),
])
def test_a_value_that_cannot_be_mapped_exactly_is_refused(edit, fragment):
    s = sections(load(INDIA))
    edit(s["uc04_equipment_access"], s["shared_configuration"])
    with pytest.raises(cc.Uc04ConfigError) as exc:
        cc.to_case_config(s, client_id="CLIENT_INDIA", role="software_engineer", grade=None, version=2)
    assert exc.value.code == cc.INVALID and fragment in exc.value.detail


# --------------------------------------------------------------------------- central read (mocked Supabase)


def test_resolve_reads_the_in_force_version_and_records_its_provenance():
    fake = FakeSupabase()
    cid = seed(fake, load(INDIA))
    resolved = resolve(fake, cid, "software_engineer")
    version_row = fake.tables["client_config_versions"][0]
    assert resolved.version == 2 and resolved.config["version"] == "v2" and resolved.client_id == cid
    assert resolved.config_hash == version_row["config_hash"] and resolved.data_origin == "test_data"
    assert resolved.provenance()["source"] == "central"
    # SELECT only, from the three central tables, and every query scoped to this client.
    assert {(m, t) for m, t, _ in fake.requests} == {("GET", "client_configs"), ("GET", "client_config_versions"),
                                                     ("GET", "client_service_policies")}
    assert all(("client_id", f"eq.{cid}") in params for _, _, params in fake.requests)


def test_mock_client_is_refused_without_the_explicit_test_data_opt_in():
    fake = FakeSupabase()
    cid = seed(fake, load(INDIA))
    with pytest.raises(cc.Uc04ConfigError) as exc:
        resolve(fake, cid, "software_engineer", environ={})
    assert exc.value.code == cc.MISSING and "mock" in exc.value.detail
    with pytest.raises(cc.Uc04ConfigError):  # production never accepts test data
        resolve(fake, cid, "software_engineer", environ={"UC02_ENV": "production", "UC02_ALLOW_SYNTHETIC_CONFIG": "1"})


@pytest.mark.parametrize("setup, code, fragment", [
    (lambda f, cid: f.tables["client_configs"].clear(), cc.MISSING, "not in client_configs"),
    (lambda f, cid: f.tables["client_configs"][0].update(is_active=False), cc.MISSING, "not active"),
    (lambda f, cid: f.tables["client_config_versions"][0].update(status="draft"), cc.MISSING, "not approved"),
    (lambda f, cid: f.tables["client_config_versions"][0].update(approved_by=None), cc.MISSING, "without an approver"),
    (lambda f, cid: f.tables["client_config_versions"][0].update(effective_from="2027-06-01"), cc.MISSING,
     "not effective"),
    (lambda f, cid: f.tables["client_service_policies"].remove(section_rows(f, cid)["uc04_equipment_access"]),
     cc.MISSING, "missing: uc04_equipment_access"),
])
def test_missing_or_unapproved_configuration_blocks_with_a_clear_reason(setup, code, fragment):
    fake = FakeSupabase()
    cid = seed(fake, load(INDIA))
    setup(fake, cid)
    with pytest.raises(cc.Uc04ConfigError) as exc:
        resolve(fake, cid, "software_engineer")
    assert exc.value.code == code and fragment in exc.value.detail


def test_tampered_or_mismatched_sections_are_refused():
    fake = FakeSupabase()
    cid = seed(fake, load(INDIA))
    section_rows(fake, cid)["uc04_equipment_access"]["policy_details"]["document"]["lead_times"]["ACCESS"] = 1
    with pytest.raises(cc.Uc04ConfigError) as tampered:  # edited after hashing
        resolve(fake, cid, "software_engineer")
    assert tampered.value.code == cc.INVALID and "sections_hash" in tampered.value.detail

    fake = FakeSupabase()
    cid = seed(fake, load(INDIA))
    for row in section_rows(fake, cid).values():
        row["policy_details"]["document_hash"] = "sha256:another-document"
    with pytest.raises(cc.Uc04ConfigError) as foreign_doc:
        resolve(fake, cid, "software_engineer")
    assert foreign_doc.value.code == cc.INVALID and "different document" in foreign_doc.value.detail


def test_a_schema_invalid_section_is_refused_even_with_consistent_hashes():
    fake = FakeSupabase()
    cid = seed(fake, load(INDIA))
    del section_rows(fake, cid)["uc04_equipment_access"]["policy_details"]["document"]["handover_rules"]
    rehash(fake, cid)
    with pytest.raises(cc.Uc04ConfigError) as exc:
        resolve(fake, cid, "software_engineer")
    assert exc.value.code == cc.INVALID and "is invalid" in exc.value.detail and "handover_rules" in exc.value.detail


@pytest.mark.parametrize("failure, code, retryable", [
    (503, cc.UNAVAILABLE, True), ("unreachable", cc.UNAVAILABLE, True), (401, cc.DENIED, False), (403, cc.DENIED, False),
])
def test_database_and_permission_failures_are_reported_not_hidden(failure, code, retryable):
    fake = FakeSupabase()
    cid = seed(fake, load(INDIA))
    fake.fail["client_configs"] = failure
    with pytest.raises(cc.Uc04ConfigError) as exc:
        resolve(fake, cid, "software_engineer")
    assert exc.value.code == code and exc.value.retryable is retryable
    assert "test-only-key" not in str(exc.value)  # never a credential in an error


# --------------------------------------------------------------------------- tenant isolation


def test_each_client_gets_only_its_own_configuration():
    fake = FakeSupabase()
    india = seed(fake, load(INDIA))
    canada = seed(fake, load(CANADA))
    a = resolve(fake, india, "software_engineer")
    b = resolve(fake, canada, "software_engineer")
    assert (a.client_id, a.config["client_id"]) == (india, india) and (b.client_id, b.config["client_id"]) == (canada, canada)
    assert a.config["role_equipment_matrix"] != b.config["role_equipment_matrix"]  # India and Canada differ
    assert a.config["work_calendar"] != b.config["work_calendar"]
    assert a.config_hash != b.config_hash
    assert b.config["country_rules"]["state"] == "VERIFIED" and a.config["country_rules"]["state"] == "MISSING"


@pytest.mark.parametrize("table", ["client_configs", "client_config_versions", "client_service_policies"])
def test_rows_of_another_client_returned_by_the_database_are_refused(table):
    """A view or policy that ignores the client filter must not hand one client's configuration to another."""
    fake = FakeSupabase()
    india = seed(fake, load(INDIA))
    seed(fake, load(CANADA))
    # Only the table under test leaks: put the other client's rows first so a careless reader would take them.
    fake.tables[table].sort(key=lambda r: r["client_id"] == india)
    fake.leak = True
    leaking = fake.__call__

    def only(method, url, headers, body):
        fake.leak = urlsplit(url).path.endswith("/" + table)
        return leaking(method, url, headers, body)

    with pytest.raises(cc.Uc04ConfigError) as exc:
        cc.resolve(rest(only), client_id=india, role="software_engineer", grade=None, joining_date=JOIN,
                   environ=TEST_DATA_OK)
    assert exc.value.code == cc.INVALID and "another client" in exc.value.detail


def test_a_role_configured_only_for_another_client_is_not_borrowed():
    fake = FakeSupabase()
    india = seed(fake, load(INDIA))
    seed(fake, load(CANADA))
    canada_only = ({m["role_id"] for m in load(CANADA)["uc04_equipment_access"]["role_equipment_matrix"]}
                   - {m["role_id"] for m in load(INDIA)["uc04_equipment_access"]["role_equipment_matrix"]}
                   - {m["role_id"] for m in load(INDIA)["uc04_equipment_access"]["role_access_matrix"]})
    assert canada_only, "the two profiles must differ for this test to mean anything"
    with pytest.raises(cc.Uc04ConfigError) as exc:
        resolve(fake, india, sorted(canada_only)[0])
    assert exc.value.code == cc.ROLE_NOT_CONFIGURED


# --------------------------------------------------------------------------- the adapter: source and precedence


class StubUc04Worker:
    """Records what the adapter sends; answers like UC04 with an empty evaluation."""

    is_alive = True

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def call(self, op, payload, *, actor, run_id=None, idempotency_key=None, timeout_s=None):
        self.calls.append(payload)
        return WorkerResponse(run_id=run_id or "r", ok=True, result={"result": {
            "case_id": payload["input"]["case_id"], "overall_status": "IN_PROGRESS", "items": [], "reassessment": None},
            "proposals": []})


class Pool:
    def __init__(self, worker):
        self.worker = worker

    def get(self, uc_id):
        assert uc_id == "uc04"
        return self.worker


def case(client_id: str, *, role: str = "software_engineer", case_id: str = "SYN-CASE-701", **extra):
    state = new_state(case_id=case_id, client_id=client_id, correlation_id="corr-uc04-central",
                      actor={"on_behalf_of": "SYN-USER-201", "roles": ["hr_onboarding_officer"]}, intake_trigger={},
                      uc02_role_context={"role_id": role, **extra.pop("role_ctx", {})}, **extra)
    state["employee_id"] = "SYN-EMP-701"
    joining_date.record_change(state, JOIN.isoformat(), stage=Stage.INTAKE, use_case_id="UC01", source="test",
                               at=now_utc().isoformat())
    return state


def deps_with(worker) -> OrchestratorDeps:
    return OrchestratorDeps(ctx=None, worker_pool=Pool(worker), uc03_repo_root=UC_ROOTS["uc03"],
                            tenants=TenantConfig.load(BASE / "HR-Onboarding-Common" / "config" / "tenants.yaml"))


@pytest.fixture
def central(monkeypatch):
    """Central mode with the test-data opt-in, the database mocked through the module's rest factory."""
    fake = FakeSupabase()
    monkeypatch.setenv(cc.SOURCE_ENV, cc.SOURCE_CENTRAL)
    for key, value in TEST_DATA_OK.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cc, "rest_factory", lambda environ: rest(fake))
    return fake


def test_central_mode_sends_the_resolved_configuration_and_records_its_source(central):
    cid = seed(central, load(INDIA))
    worker = StubUc04Worker()
    out = call_uc04_equipment_access(case(cid), deps_with(worker))
    sent = worker.calls[0]["input"]["config"]
    assert sent == resolve(central, cid, "software_engineer").config
    result = out["equipment_access_result"]
    assert result["config_source"]["source"] == "central" and result["config_source"]["version"] == 2
    assert result["config_version"] == "v2"


def test_central_mode_never_falls_back_to_the_tenant_file(central):
    """CLIENT-ALPHA has JSON files in tenants.yaml but no central configuration: central mode blocks."""
    worker = StubUc04Worker()
    out = call_uc04_equipment_access(case("CLIENT-ALPHA"), deps_with(worker))
    assert out["stage_statuses"][S] == StageStatus.BLOCKED.value
    assert out["stage_errors"][S]["code"] == cc.MISSING and worker.calls == []


def test_central_mode_refuses_a_case_file_reference(central):
    seed(central, load(INDIA))
    worker = StubUc04Worker()
    ref = str(UC_ROOTS["uc04"] / "data" / "client_alpha_config.json")
    out = call_uc04_equipment_access(case("CLIENT_INDIA", uc04_client_config_ref=ref), deps_with(worker))
    assert out["stage_errors"][S]["code"] == cc.SOURCE_CONFLICT and worker.calls == []


@pytest.mark.parametrize("failure, code", [(503, cc.UNAVAILABLE), (403, cc.DENIED)])
def test_central_mode_database_failures_block_the_stage(central, failure, code):
    seed(central, load(INDIA))
    central.fail["client_config_versions"] = failure
    worker = StubUc04Worker()
    out = call_uc04_equipment_access(case("CLIENT_INDIA"), deps_with(worker))
    assert out["stage_statuses"][S] == StageStatus.BLOCKED.value
    assert out["stage_errors"][S]["code"] == code and worker.calls == []


def test_an_unknown_source_setting_is_refused(monkeypatch):
    monkeypatch.setenv(cc.SOURCE_ENV, "database")
    worker = StubUc04Worker()
    out = call_uc04_equipment_access(case("CLIENT-ALPHA"), deps_with(worker))
    assert out["stage_errors"][S]["code"] == cc.SOURCE_INVALID and worker.calls == []


def test_file_mode_is_the_default_and_unchanged(monkeypatch):
    monkeypatch.delenv(cc.SOURCE_ENV, raising=False)
    monkeypatch.setattr(cc, "rest_factory", lambda environ: pytest.fail("file mode must not read the database"))
    worker = StubUc04Worker()
    out = call_uc04_equipment_access(case("CLIENT-ALPHA"), deps_with(worker))
    assert worker.calls and worker.calls[0]["input"]["config"]["client_id"] == "CLIENT-ALPHA"
    assert out["equipment_access_result"]["config_source"] == {"source": "file",
                                                               "file": "CLIENT-ALPHA.software_engineer.json"}


# --------------------------------------------------------------------------- the real UC04 accepts it


@pytest.fixture
def platform_uc04_worker(monkeypatch):
    """The real UC04 worker under its PLATFORM identifier profile (real client ids such as CLIENT_INDIA)."""
    require_uc("uc04")
    from hr_onboarding_common.orchestration.workers.config import WorkerConfig
    from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker

    monkeypatch.setenv("UC04_IDENTITY_PROFILE", "PLATFORM")
    worker = UcWorker(WorkerConfig(uc_id="uc04", python_executable=venv_python(UC_ROOTS["uc04"]),
                                   src_dir=UC_ROOTS["uc04"] / "src"))
    worker.start()
    yield worker
    worker.stop()


def test_the_real_uc04_evaluates_against_the_central_configuration(central, platform_uc04_worker):
    """UC04's own strict CaseConfig accepts the mapped configuration, and its rules use it. Both items first need the
    standard approval of the role India's central approver_config names; once given, the item in India's matrix goes
    ahead and the one outside it (monitor-24: Canada's matrix has it, India's does not) still needs an exception (GR-03)."""
    from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import uc04_approval_record

    india = load(INDIA)["uc04_equipment_access"]
    cid = seed(central, load(INDIA))
    in_matrix = next(m for m in india["role_equipment_matrix"] if m["role_id"] == "software_engineer")["items"][0]
    assert all("monitor-24" not in m["items"] for m in india["role_equipment_matrix"])
    deps = deps_with(platform_uc04_worker)
    state = case(cid, case_id=str(uuid.uuid4()), role_ctx={"requested_equipment": [in_matrix, "monitor-24"]})
    state["employee_id"] = "EMP-000701"

    first = call_uc04_equipment_access(state, deps)
    assert first["stage_statuses"][S] != StageStatus.FAILED.value, first.get("stage_errors")
    result = first["equipment_access_result"]
    assert result["config_source"]["source"] == "central" and result["config_version"] == "v2"
    assert {i["item_id"]: i["state"] for i in result["items"]} == {in_matrix: "APPROVAL_PENDING",
                                                                    "monitor-24": "APPROVAL_PENDING"}
    pending = [p for p in first["pending_approvals"] if p.get("proposal_type") == "uc04_item_approval"]
    assert {p["role"] for p in pending} == {india["approver_config"]["EQUIPMENT"]["role"]}  # from the central config

    first["uc02_role_context"]["approval_records"] = [
        uc04_approval_record(first, p, decision="approved", approver_id="manager-701") for p in pending]
    second = call_uc04_equipment_access(first, deps)
    assert second["stage_statuses"][S] != StageStatus.FAILED.value, second.get("stage_errors")
    states = {i["item_id"]: i["state"] for i in second["equipment_access_result"]["items"]}
    assert states["monitor-24"] == "EXCEPTION_PENDING"  # out of India's matrix: the exception approval is still needed
    assert states[in_matrix] not in ("APPROVAL_PENDING", "EXCEPTION_PENDING")  # in the matrix: standard approval suffices
