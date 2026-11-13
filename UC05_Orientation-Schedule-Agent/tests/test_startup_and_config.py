"""Application startup and configuration validation. api.py builds its context at import time, so
each case runs in a fresh interpreter with its own environment (HR_AGENT_ENV=test and header identity
unless a case says otherwise; tests/test_identity.py covers IDENTITY_MODE=jwt)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SERVE = """
from fastapi.testclient import TestClient
import sub_agent.api as api
headers = {"X-User-Id": "user:hr-admin-01", "X-User-Roles": "hr_admin", "X-Client-Id": "CLIENT-ACME"}
with TestClient(api.app) as c:
    print(c.get("/healthz").status_code, c.get("/openapi.json").status_code,
          c.get("/v1/onboarding/EMP-001", headers=headers).status_code,
          c.post("/v1/runs", headers=headers, json={"instance": "EMP-001", "input": {"employee_id": "EMP-001"}}).status_code,
          c.post("/v1/onboarding/workflow/policy-questions", headers=headers,
                 json={"question": "What is the dress code?", "as_of": "2026-11-01"}).status_code)
"""


def _python(code: str, **env: str | None) -> subprocess.CompletedProcess:
    """`env` overrides the defaults below; a value of None removes the variable."""
    full_env = {k: v for k, v in os.environ.items() if not k.startswith(("HR_AGENT_", "IDENTITY_", "SCHEDULER_"))}
    for name in ("CONNECTOR_MODE", "AGENT_HOME"):
        full_env.pop(name, None)
    paths = [str(ROOT), str(ROOT / "src")]
    common_src = ROOT.parent / "HR-Onboarding-Common" / "src"
    if common_src.exists():
        paths.append(str(common_src))
    full_env.update({"PYTHONPATH": os.pathsep.join(paths), "LOG_LEVEL": "ERROR",
                     "HR_AGENT_ENV": "test", "IDENTITY_MODE": "insecure-dev-headers", "HR_AGENT_PERSIST": "0"})
    for name, value in env.items():
        if value is None:
            full_env.pop(name, None)
        else:
            full_env[name] = value
    # A developer's local .env (see .env.example) must never leak into these subprocesses: every
    # case here simulates one exact environment, and sub_agent.api/sub_agent.scheduler call
    # load_dotenv() on import. find_dotenv() locates .env by walking up from api.py's own path on
    # disk, so it is found regardless of cwd or which vars this function removes above; disabling
    # it here keeps "a variable is absent" meaning exactly that, independent of local dev setup.
    guard = "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
    return subprocess.run([sys.executable, "-c", guard + code], capture_output=True, text=True, cwd=ROOT,
                          env=full_env, timeout=180)


def test_app_starts_and_serves_with_the_default_offline_configuration():
    out = _python(SERVE)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split()[-5:] == ["200", "200", "200", "200", "200"]


def test_app_persists_to_the_configured_data_dir_when_dact_persist_is_set(tmp_path):
    out = _python(SERVE, HR_AGENT_PERSIST="1", HR_AGENT_DATA_DIR=str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert {"onboarding.db", "hitl.db", "checkpoints.db", "audit.jsonl"} <= {p.name for p in tmp_path.iterdir()}


@pytest.mark.parametrize("name, value", [("CONNECTOR_MODE", "live")])
def test_unsupported_integration_modes_fail_startup_instead_of_pretending(name, value):
    out = _python("import sub_agent.api", **{name: value})
    assert out.returncode != 0
    assert f"{name}='{value}' is not supported" in out.stderr


def test_missing_policy_envelope_fails_startup_with_guidance(tmp_path):
    out = _python("import sub_agent.api", AGENT_HOME=str(tmp_path))  # no guardrails/ there
    assert out.returncode != 0
    assert "no policy envelope for 'employee-onboarding'" in out.stderr and "AGENT_HOME" in out.stderr


# Phase 4 (finding A): the former HR_AGENT_ALLOW_INSECURE_HEADER_IDENTITY switch defaulted to ON. It is
# gone; header identity now needs IDENTITY_MODE=insecure-dev-headers AND HR_AGENT_ENV development/test.

@pytest.mark.parametrize("dact_env", ["staging", "production", None])  # None: HR_AGENT_ENV unset = production
def test_header_identity_is_refused_at_startup_outside_development_and_test(dact_env):
    out = _python("import sub_agent.api", HR_AGENT_ENV=dact_env)
    assert out.returncode != 0
    assert "IDENTITY_MODE=insecure-dev-headers is refused" in out.stderr


def test_without_identity_configuration_the_default_jwt_mode_fails_startup():
    out = _python("import sub_agent.api", IDENTITY_MODE=None)
    assert out.returncode != 0
    assert "IDENTITY_JWT_ISSUER and IDENTITY_JWT_AUDIENCE are required" in out.stderr


def test_the_retired_header_identity_switch_fails_startup_instead_of_being_ignored():
    out = _python("import sub_agent.api", HR_AGENT_ALLOW_INSECURE_HEADER_IDENTITY="1")
    assert out.returncode != 0
    assert "replaced by IDENTITY_MODE" in out.stderr


JWT_ENV = {"IDENTITY_MODE": "jwt", "IDENTITY_JWT_ISSUER": "https://idp.test.invalid/",
           "IDENTITY_JWT_AUDIENCE": "hr-onboarding-agent",
           "IDENTITY_JWT_PUBLIC_KEY": "-----BEGIN PUBLIC KEY-----\nnot-used-at-startup\n-----END PUBLIC KEY-----"}


@pytest.mark.parametrize("dact_env, persist, message", [
    ("staging", "0", "HR_AGENT_ENV=staging requires HR_AGENT_PERSIST=1"),
    ("production", "0", "HR_AGENT_ENV=production requires HR_AGENT_PERSIST=1"),
    ("production", "1", "HR_AGENT_ENV=production refuses fake connectors"),
    ("qa", "1", "is not one of"),
])
def test_non_development_environments_refuse_in_memory_data_and_production_refuses_fakes(tmp_path, dact_env,
                                                                                         persist, message):
    out = _python("import sub_agent.api", HR_AGENT_ENV=dact_env, HR_AGENT_PERSIST=persist, HR_AGENT_DATA_DIR=str(tmp_path),
                  **JWT_ENV)
    assert out.returncode != 0
    assert message in out.stderr


def test_staging_runs_on_persistent_storage_without_seeding_synthetic_data(tmp_path):
    code = ("from fastapi.testclient import TestClient\nimport sub_agent.api as api\n"
            "with TestClient(api.app) as c:\n    print(c.get('/healthz').status_code, "
            "len(api.ctx.connector.repository.list_onboarding_records()))")
    out = _python(code, HR_AGENT_ENV="staging", HR_AGENT_PERSIST="1", HR_AGENT_DATA_DIR=str(tmp_path), **JWT_ENV)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split()[-2:] == ["200", "0"]  # synthetic demo data is only ever seeded in development/test
    assert (tmp_path / "onboarding.db").exists()
