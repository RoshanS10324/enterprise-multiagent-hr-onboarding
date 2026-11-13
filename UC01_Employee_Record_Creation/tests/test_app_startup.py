"""The default app is built lazily, and still fails closed at startup.

Importing sub_agent.api must not need credentials; starting a server with a
refused configuration must not serve anything.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sub_agent.api.app import LazyDefaultApp

SRC = Path(__file__).resolve().parent.parent / "src"
SUPABASE_WITHOUT_KEY = {"SUPABASE_URL": "https://example-project.supabase.co",
                        "SUPABASE_SERVICE_ROLE_KEY": "test-only", "UC01_FIELD_ENCRYPTION_KEY": ""}


def _env(**over: str) -> dict[str, str]:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    env.update(over)
    return env


def test_importing_the_api_needs_no_credentials():
    """A fresh interpreter with an incomplete Supabase configuration (a
    preset variable wins over .env) imports cleanly and builds nothing."""
    proc = subprocess.run(
        [sys.executable, "-c", "import sub_agent.api as a; assert a.app._app is None; print('ok')"],
        env=_env(**SUPABASE_WITHOUT_KEY), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert proc.stdout.strip() == "ok"


def test_startup_fails_closed_when_the_encryption_key_is_missing(monkeypatch):
    for k, v in SUPABASE_WITHOUT_KEY.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(BaseException) as exc:
        with TestClient(LazyDefaultApp()):
            pass  # pragma: no cover - startup must not succeed
    text = str(exc.value)
    assert "UC01_FIELD_ENCRYPTION_KEY" in text
    assert "input_value" not in text and "test-only" not in text  # names, never values


def test_production_without_persistence_fails_closed(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("APP_ENV", "production")
    # A developer's local .env may hold a dev-... key id, which production refuses
    # for its own reason; this test is about the missing persistence.
    monkeypatch.delenv("UC01_FIELD_ENCRYPTION_KEY_ID", raising=False)
    monkeypatch.delenv("UC01_FIELD_ENCRYPTION_PREVIOUS_KEYS", raising=False)
    with pytest.raises(BaseException) as exc:
        with TestClient(LazyDefaultApp()):
            pass  # pragma: no cover
    assert "requires durable persistence" in str(exc.value)


def test_a_request_without_a_valid_configuration_gets_503(monkeypatch):
    for k, v in SUPABASE_WITHOUT_KEY.items():
        monkeypatch.setenv(k, v)
    response = TestClient(LazyDefaultApp()).get("/health")  # no lifespan: plain request
    assert response.status_code == 503 and response.json() == {"detail": "UC-01 is not configured"}


def test_dev_configuration_builds_once_and_serves(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("APP_ENV", "dev")
    calls = []

    def factory():
        from sub_agent.api.app import _build_default_app

        calls.append(1)
        return _build_default_app()

    lazy = LazyDefaultApp(factory)
    with TestClient(lazy) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200
    assert calls == [1]
    assert hasattr(lazy, "router")  # attribute access reaches the built app
