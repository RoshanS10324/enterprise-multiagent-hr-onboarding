"""Offline tests for the UC-04 Supabase connector: no network, no credentials. Run from this folder with UC-04's interpreter:

    ..\\..\\..\\.venv\\Scripts\\python.exe -m pytest -p no:cacheprovider test_connector.py
"""

import json
import urllib.error
from pathlib import Path

import pytest

import connect
import postgrest_table_client as ptc
from postgrest_table_client import DatabaseError, PostgrestTableClient

KEY = "k" * 40


class FakeResponse:
    def __init__(self, status=200, body=b"[]"):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append({"method": request.get_method(), "url": request.full_url, "headers": dict(request.header_items()),
                     "body": json.loads(request.data) if request.data else None})
        return FakeResponse(body=json.dumps([{"case_id": "c"}]).encode())

    monkeypatch.setattr(ptc.urllib.request, "urlopen", fake_urlopen)
    return seen


def client():
    return PostgrestTableClient("https://project.example.com", KEY, "public")


def test_select_is_always_filtered_and_builds_a_safe_query(calls):
    rows = client().select("onboarding_cases", filters={"case_id": "a b&c"}, columns=["case_id", "client_id"], limit=2)
    assert rows == [{"case_id": "c"}] and calls[0]["method"] == "GET"
    assert "case_id=eq.a%20b%26c" in calls[0]["url"] and "select=case_id,client_id" in calls[0]["url"] and "limit=2" in calls[0]["url"]
    with pytest.raises(ValueError):
        client().select("onboarding_cases", filters={})


def test_update_is_a_filtered_compare_and_swap_and_returns_the_row_count(calls):
    assert client().update("uc04_case_state", filters={"case_id": "c", "version": 3}, values={"version": 4}) == 1
    assert calls[0]["method"] == "PATCH" and "version=eq.3" in calls[0]["url"] and calls[0]["body"] == {"version": 4}
    with pytest.raises(ValueError):
        client().update("uc04_case_state", filters={}, values={"version": 1})


def test_insert_and_upsert_use_the_documented_preferences(calls):
    client().insert("audit_events", {"action": "x"})
    client().upsert("agent_runs", {"run_id": "r"}, on_conflict=("run_id",))
    assert calls[0]["method"] == "POST" and calls[0]["headers"]["Prefer"] == "return=minimal"
    assert "on_conflict=run_id" in calls[1]["url"] and calls[1]["headers"]["Prefer"].startswith("resolution=merge-duplicates")


def test_there_is_no_delete_operation():
    assert not any(hasattr(PostgrestTableClient, name) for name in ("delete", "truncate", "drop"))


def test_errors_carry_a_status_and_code_only_never_the_key_url_or_row(monkeypatch):
    def boom(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 409, "conflict", {}, __import__("io").BytesIO(b'{"code":"23505","message":"row SECRET-ROW-DATA"}'))

    monkeypatch.setattr(ptc.urllib.request, "urlopen", boom)
    with pytest.raises(DatabaseError) as e:
        client().insert("onboarding_cases", {"candidate_name": "SECRET-ROW-DATA"})
    text = str(e.value)
    assert "409" in text and "23505" in text and KEY not in text and "project.example.com" not in text and "SECRET-ROW-DATA" not in text


def test_a_network_failure_is_reported_by_its_class_only(monkeypatch):
    monkeypatch.setattr(ptc.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("host project.example.com refused")))
    with pytest.raises(DatabaseError) as e:
        client().select("onboarding_cases", filters={"case_id": "c"})
    assert "project.example.com" not in str(e.value) and "URLError" in str(e.value)


def test_table_exists_distinguishes_missing_from_a_real_error(monkeypatch):
    def respond(status):
        def _open(request, timeout=None):
            if status == 200:
                return FakeResponse()
            raise urllib.error.HTTPError(request.full_url, status, "x", {}, __import__("io").BytesIO(b"{}"))
        return _open

    monkeypatch.setattr(ptc.urllib.request, "urlopen", respond(200))
    assert client().table_exists("t") is True
    monkeypatch.setattr(ptc.urllib.request, "urlopen", respond(404))
    assert client().table_exists("t") is False
    monkeypatch.setattr(ptc.urllib.request, "urlopen", respond(401))
    with pytest.raises(DatabaseError):
        client().table_exists("t")


def test_settings_prefer_the_environment_then_the_env_file_and_report_only_names(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("SUPABASE_URL=https://from-file.example.com\nSUPABASE_SERVICE_ROLE_KEY=filekey\n", encoding="utf-8")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "envkey")
    monkeypatch.delenv("SUPABASE_SCHEMA", raising=False)
    s = connect.load_settings(env_file)
    assert s["SUPABASE_URL"] == "https://from-file.example.com" and s["SUPABASE_SERVICE_ROLE_KEY"] == "envkey" and s["SUPABASE_SCHEMA"] == ""


def test_missing_credentials_are_refused_not_defaulted():
    with pytest.raises(connect.NotReady):
        connect.make_client({"SUPABASE_URL": "", "SUPABASE_SERVICE_ROLE_KEY": ""})


class FakeTables:
    """Stands in for the client in build_agent tests: reports which tables exist."""

    def __init__(self, existing):
        self.existing = set(existing)

    def table_exists(self, name):
        return name in self.existing


def test_supabase_state_mode_refuses_to_start_when_the_uc04_tables_are_missing_and_never_falls_back():
    with pytest.raises(connect.NotReady) as e:
        connect.build_agent(state_mode="supabase", client=FakeTables({"onboarding_cases"}))
    assert "uc04_case_state" in str(e.value)


def test_an_unknown_state_mode_is_refused():
    with pytest.raises(ValueError):
        connect.build_agent(state_mode="sqlite", client=FakeTables(set()))


def test_the_connector_changes_no_other_agent_and_imports_only_uc04():
    text = "".join(p.read_text(encoding="utf-8") for p in Path(connect.__file__).parent.glob("*.py") if not p.name.startswith("test_"))
    for other in ("hr_onboarding_common", "onboarding_task_management", "UC01_", "UC02_", "UC03_", "UC05_"):
        assert other not in text
