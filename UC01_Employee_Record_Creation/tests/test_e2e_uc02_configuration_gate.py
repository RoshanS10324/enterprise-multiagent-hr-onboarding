"""UC-02 is triggered only with the APPROVED configuration of the saved employee's OWN client.

UC-01 storage: the real Supabase adapters over a FAKE PostgREST (MOCK PASS).
UC-02's configuration: UC-02's real SupabaseConfigSource/Registry reading an
in-test fake REST source, so the refusals below are UC-02's own (draft,
missing templates, invalid templates); nothing in UC-02 is changed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("onboarding_task_management")

from test_e2e_uc01_to_uc02 import ALPHA_FIXTURE, STEP, _fake_supabase, _fixture, _run_fake, e2e, env  # noqa: E402,F401

CLIENT = "SYN-CFG-TEST"
ENVIRON = {"UC02_ENV": "development", "UC02_ALLOW_SYNTHETIC_CONFIG": "1"}


class FakeRest:
    """The few PostgREST reads UC-02's configuration source makes."""

    def __init__(self, tables):
        self.tables = tables

    def select(self, table, filters, order_by=None, *, columns="*", limit=None, extra=()):
        rows = [r for r in self.tables.get(table, [])
                if all(str(r.get(k)).lower() == str(v).lower() for k, v in filters.items())]
        if order_by:
            column, _, direction = order_by.partition(".")
            rows.sort(key=lambda r: r[column], reverse=direction == "desc")
        return rows[:limit] if limit else rows


def _template(key, **over):
    row = dict(template_key=key, client_id=CLIENT, category="hr_record", title=key.replace("_", " ").title(),
               title_ar="", owner_role="hr_ops", default_lead_days=3, evidence_required=False,
               requires_attributes=[], sensitive_to=[], approval_role=None, description="SYNTHETIC",
               role_ids=[], grades=[], locations=[], projects=[], nationality_groups=[],
               depends_on_template_keys=[], offset_days=-5, default_priority="normal", config_version=1,
               is_active=True)
    row.update(over)
    return row


def _tables(status="approved", templates=None):
    return {
        "client_configs": [{"client_id": CLIENT, "company_name": "SYNTHETIC", "config_hash": "synthetic-cfg",
                            "working_days": [1, 2, 3, 4, 5], "timezone": "Asia/Dubai", "is_active": True,
                            "settings_payload": {"data_origin": "synthetic"}}],
        "client_config_versions": [{"client_id": CLIENT, "version": 1, "effective_from": "2026-01-01",
                                    "effective_to": None, "status": status, "config_hash": "synthetic-cfg",
                                    "approved_by": "synthetic-fixture" if status == "approved" else None,
                                    "approved_at": "2026-01-01T00:00:00+00:00" if status == "approved" else None}],
        "task_templates": templates if templates is not None else [
            _template("hr_record_create"), _template("collect_documents", depends_on_template_keys=["hr_record_create"])],
    }


@pytest.fixture
def uc02_config(monkeypatch):
    """Point UC-02's runtime at its real Supabase configuration reader over FakeRest."""
    from onboarding_task_management.sub_agent import runtime
    from onboarding_task_management.supabase_config import SupabaseConfigRegistry, SupabaseConfigSource

    def install(tables):
        source = SupabaseConfigSource(FakeRest(tables), allow_synthetic=True, environ=ENVIRON)
        registry = SupabaseConfigRegistry(source=source)
        registry.cache = {}
        registry.configs = {}
        monkeypatch.setattr(runtime, "build_service", lambda **_: SimpleNamespace(registry=registry))

    return install


def _run(env, **mutate):
    path = _fixture(env, lambda d: d["intake"].update(client_id=CLIENT), source=ALPHA_FIXTURE)
    fake, container = _fake_supabase()
    agent, deps = e2e.orchestrator()
    report = _run_fake(container, fixture_path=path, agent=agent, deps=deps)
    return report, deps, fake


def _blocked(report, deps):
    step = report["steps"][STEP["7"]]
    assert step["status"] == "BLOCKED", step
    assert "UC-02 NOT triggered" in report["stopped"] and deps._uc02_sub_agent is None
    assert report["uc02_gate"] == {"employee_saved_to_supabase": True, "employee_readback_verified": True}
    return step["configuration_error"]


def test_the_approved_configuration_of_the_same_client_is_retrieved(env, uc02_config):
    uc02_config(_tables())
    cfg = e2e.uc02_configuration(CLIENT, "2027-02-01")
    assert cfg["client_id"] == CLIENT and cfg["version"] == "1" and cfg["approval_status"] == "approved"
    assert cfg["template_keys"] == ["collect_documents", "hr_record_create"]


def test_a_missing_client_configuration_stops_before_uc02(env, uc02_config):
    uc02_config({**_tables(), "client_configs": []})
    report, deps, _ = _run(env)
    assert "not in client_configs" in _blocked(report, deps)


def test_a_draft_configuration_stops_before_uc02(env, uc02_config):
    uc02_config(_tables(status="draft"))
    report, deps, _ = _run(env)
    assert "no approved configuration version" in _blocked(report, deps)


def test_missing_task_templates_stop_before_uc02(env, uc02_config):
    uc02_config(_tables(templates=[]))
    report, deps, _ = _run(env)
    assert "no active task templates" in _blocked(report, deps)


def test_a_template_without_an_owner_stops_before_uc02(env, uc02_config):
    uc02_config(_tables(templates=[_template("hr_record_create", owner_role=None)]))
    report, deps, _ = _run(env)
    _blocked(report, deps)


def test_a_dependency_on_a_missing_template_stops_before_uc02(env, uc02_config):
    uc02_config(_tables(templates=[_template("collect_documents", depends_on_template_keys=["no_such_template"])]))
    report, deps, _ = _run(env)
    _blocked(report, deps)


def test_a_configuration_of_a_different_client_is_never_used(env, monkeypatch):
    from onboarding_task_management.sub_agent import runtime

    other = SimpleNamespace(client_id="CLIENT-ALPHA", version="1", approval_status="approved", templates=(),
                            milestones=())
    monkeypatch.setattr(runtime, "build_service",
                        lambda **_: SimpleNamespace(registry=SimpleNamespace(get=lambda client_id, on: other)))
    report, deps, _ = _run(env)
    assert "configuration of 'CLIENT-ALPHA'" in _blocked(report, deps)


def test_the_sample_employee_states_its_checklist_attributes(env):
    fixture = json.loads(e2e.FIXTURE.read_text(encoding="utf-8"))
    rb = {"client_id": "SYN-UAE-TEST", "candidate_name": "Rania Synthetic", "candidate_email": "x@example.invalid",
          "designation": "Synthetic Platform Engineer", "joining_date": "2027-02-01", "work_location_type": "OFFICE",
          "employee_no": "EMP-000001", "department": "technology", "grade": "L4", "nationality_group": "grp-a",
          "form_approved_at": "2026-11-01T09:00:00+00:00"}
    inp = e2e.handoff(rb, fixture)
    assert inp.client_id == "SYN-UAE-TEST" and inp.grade == "L4"
    assert inp.attributes == fixture["intake"]["uc02"]["attributes"]  # passed as stated, nothing derived
