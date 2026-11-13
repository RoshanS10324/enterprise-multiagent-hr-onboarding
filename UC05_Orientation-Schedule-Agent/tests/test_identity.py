"""Verified caller identity (Phase 4, finding A).

Keys are generated in memory for each test session -- nothing is committed, and no real identity
provider is involved. The API cases run in a fresh interpreter (api.py configures identity at
import time) with IDENTITY_MODE=jwt; they show that request headers cannot supply a user, a role or
a tenant, so no header change turns a caller into an HR administrator.
"""

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from hr_onboarding_common.platform.identity import IdentityError, InsecureHeaderIdentityProvider
from sub_agent.identity import (IdentityConfigurationError, JwtIdentityProvider, JwtSettings,
                                build_identity_provider)

ROOT = Path(__file__).resolve().parents[1]
ISSUER, AUDIENCE = "https://idp.test.invalid/", "hr-onboarding-agent"


def _pem_public(key) -> str:
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()


@pytest.fixture(scope="module")
def keys():
    return {"rsa": rsa.generate_private_key(public_exponent=65537, key_size=2048),
            "other": rsa.generate_private_key(public_exponent=65537, key_size=2048),
            "ec": ec.generate_private_key(ec.SECP256R1())}


def token(key, *, alg="RS256", kid=None, **overrides) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "user:hr-admin-01", "iat": now, "exp": now + 300,
              "client_id": "CLIENT-ACME", "roles": ["hr_admin"], **overrides}
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid} if kid else None)


def bearer(value: str) -> dict[str, str]:
    return {"authorization": f"Bearer {value}"}


@pytest.fixture
def provider(keys):
    return JwtIdentityProvider(JwtSettings(issuer=ISSUER, audience=AUDIENCE, public_key_pem=_pem_public(keys["rsa"])))


# =========================================================================== token verification

def test_a_valid_token_yields_the_signed_user_roles_and_tenant(provider, keys):
    actor = provider.authenticate(bearer(token(keys["rsa"], roles=["team_lead"])))
    assert (actor.user_id, actor.roles, actor.client_id, actor.auth_method) == (
        "user:hr-admin-01", ["team_lead"], "CLIENT-ACME", "jwt")


def test_identity_headers_are_ignored_even_alongside_a_valid_token(provider, keys):
    headers = {**bearer(token(keys["rsa"], roles=["employee"])), "x-user-id": "user:mallory",
               "x-user-roles": "hr_admin", "x-client-id": "CLIENT-GLOBEX"}
    actor = provider.authenticate(headers)
    assert (actor.user_id, actor.roles, actor.client_id) == ("user:hr-admin-01", ["employee"], "CLIENT-ACME")


@pytest.mark.parametrize("headers", [
    {}, {"x-user-id": "user:hr-admin-01", "x-user-roles": "hr_admin", "x-client-id": "CLIENT-ACME"},
    {"authorization": "Basic dXNlcjpwYXNz"}, {"authorization": "Bearer "}, {"authorization": "Bearer not.a.jwt"}])
def test_missing_or_malformed_credentials_are_rejected(provider, headers):
    with pytest.raises(IdentityError):
        provider.authenticate(headers)


@pytest.mark.parametrize("case, overrides, message", [
    ("expired", {"exp": int(time.time()) - 120, "iat": int(time.time()) - 600}, "token expired"),
    ("wrong issuer", {"iss": "https://evil.test.invalid/"}, "InvalidIssuerError"),
    ("wrong audience", {"aud": "some-other-service"}, "InvalidAudienceError"),
    ("not yet valid", {"nbf": int(time.time()) + 600}, "ImmatureSignatureError"),
    ("no subject", {"sub": None}, "MissingRequiredClaimError"),
    ("no expiry", {"exp": None}, "MissingRequiredClaimError"),
    ("too old", {"iat": int(time.time()) - 7200, "exp": int(time.time()) + 300}, "token too old"),
    ("no tenant", {"client_id": None}, "no 'client_id' claim"),
    ("blank tenant", {"client_id": "  "}, "no 'client_id' claim"),
    ("roles not a list", {"roles": "hr_admin"}, "must be a list of strings"),
])
def test_invalid_claims_are_rejected(provider, keys, case, overrides, message):
    with pytest.raises(IdentityError, match=message):
        provider.authenticate(bearer(token(keys["rsa"], **overrides)))


def test_a_token_signed_by_another_key_is_rejected(provider, keys):
    with pytest.raises(IdentityError, match="InvalidSignatureError"):
        provider.authenticate(bearer(token(keys["other"])))


def test_unsigned_and_symmetric_tokens_are_rejected_including_algorithm_confusion(provider, keys):
    unsigned = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "u", "iat": int(time.time()),
                           "exp": int(time.time()) + 60, "client_id": "CLIENT-ACME", "roles": ["hr_admin"]},
                          None, algorithm="none")
    # HS256 "signed" with the public key as the shared secret: the classic RS/HS confusion attack.
    # Built by hand -- PyJWT itself refuses to use a PEM key as an HMAC secret.
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps(jwt.decode(token(keys["rsa"]), options={"verify_signature": False})).encode())
    signature = hmac.new(_pem_public(keys["rsa"]).encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    confused = f"{header}.{payload}.{b64(signature)}"
    for value in (unsigned, confused):
        with pytest.raises(IdentityError):
            provider.authenticate(bearer(value))


def test_a_token_whose_algorithm_is_not_configured_is_rejected(keys):
    rs_only = JwtIdentityProvider(JwtSettings(issuer=ISSUER, audience=AUDIENCE, algorithms=("RS256",),
                                              public_key_pem=_pem_public(keys["ec"])))
    with pytest.raises(IdentityError):
        rs_only.authenticate(bearer(token(keys["ec"], alg="ES256")))


def test_a_custom_tenant_and_roles_claim_can_be_configured(keys):
    provider = JwtIdentityProvider(JwtSettings(issuer=ISSUER, audience=AUDIENCE, public_key_pem=_pem_public(keys["rsa"]),
                                               tenant_claim="tid", roles_claim="groups"))
    actor = provider.authenticate(bearer(token(keys["rsa"], client_id=None, roles=None, tid="CLIENT-ACME",
                                               groups=["hr_admin"])))
    assert (actor.client_id, actor.roles) == ("CLIENT-ACME", ["hr_admin"])


def test_keys_can_come_from_a_jwks_endpoint(keys):
    public = keys["rsa"].public_key()
    jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public)), "kid": "k1", "use": "sig", "alg": "RS256"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"keys": [jwk]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        provider = JwtIdentityProvider(JwtSettings(issuer=ISSUER, audience=AUDIENCE,
                                                   jwks_url=f"http://127.0.0.1:{server.server_port}/jwks"))
        assert provider.authenticate(bearer(token(keys["rsa"], kid="k1"))).client_id == "CLIENT-ACME"
        with pytest.raises(IdentityError, match="signing key could not be resolved"):
            provider.authenticate(bearer(token(keys["other"], kid="unknown-kid")))
    finally:
        server.shutdown()


# =========================================================================== configuration

@pytest.mark.parametrize("kwargs, message", [
    ({"issuer": "", "public_key_pem": "x"}, "IDENTITY_JWT_ISSUER and IDENTITY_JWT_AUDIENCE are required"),
    ({"algorithms": ("HS256",), "public_key_pem": "x"}, "only asymmetric"),
    ({"algorithms": ("RS256", "none"), "public_key_pem": "x"}, "only asymmetric"),
    ({}, "exactly one of"),
    ({"public_key_pem": "x", "jwks_url": "https://idp.test.invalid/jwks"}, "exactly one of"),
])
def test_unsafe_or_incomplete_jwt_settings_are_refused(kwargs, message):
    with pytest.raises(IdentityConfigurationError, match=message):
        JwtSettings(**{"issuer": ISSUER, "audience": AUDIENCE, **kwargs})


@pytest.mark.parametrize("dact_env", ["production", "staging", "PRODUCTION", ""])
def test_header_identity_cannot_be_enabled_outside_development_and_test(dact_env):
    env = {"IDENTITY_MODE": "insecure-dev-headers"}
    if dact_env:
        env["HR_AGENT_ENV"] = dact_env
    with pytest.raises(IdentityConfigurationError, match="refused"):
        build_identity_provider(env)


@pytest.mark.parametrize("dact_env", ["development", "test"])
def test_header_identity_is_available_for_development_and_test(dact_env):
    provider, mode = build_identity_provider({"IDENTITY_MODE": "insecure-dev-headers", "HR_AGENT_ENV": dact_env})
    assert mode == "insecure-dev-headers" and isinstance(provider, InsecureHeaderIdentityProvider)


def test_jwt_is_the_default_mode_and_requires_its_configuration(keys, tmp_path):
    with pytest.raises(IdentityConfigurationError, match="IDENTITY_JWT_ISSUER"):
        build_identity_provider({})
    key_file = tmp_path / "idp.pem"
    key_file.write_text(_pem_public(keys["rsa"]), encoding="utf-8")
    provider, mode = build_identity_provider({"IDENTITY_JWT_ISSUER": ISSUER, "IDENTITY_JWT_AUDIENCE": AUDIENCE,
                                              "IDENTITY_JWT_PUBLIC_KEY_FILE": str(key_file)})
    assert mode == "jwt" and provider.authenticate(bearer(token(keys["rsa"]))).client_id == "CLIENT-ACME"


@pytest.mark.parametrize("env, message", [
    ({"HR_AGENT_ALLOW_INSECURE_HEADER_IDENTITY": "1"}, "replaced by IDENTITY_MODE"),
    ({"IDENTITY_MODE": "anonymous"}, "unknown IDENTITY_MODE"),
])
def test_retired_or_unknown_identity_switches_are_refused(env, message):
    with pytest.raises(IdentityConfigurationError, match=message):
        build_identity_provider(env)


# =========================================================================== the API in jwt mode

API_SCRIPT = r"""
import json, os
from fastapi.testclient import TestClient
import sub_agent.api as api

t = json.loads(os.environ["TEST_TOKENS"])
def auth(name, **headers):
    return {"Authorization": "Bearer " + t[name], **headers}
spoof = {"X-User-Id": "user:hr-admin-01", "X-User-Roles": "hr_admin", "X-Client-Id": "CLIENT-ACME"}
out = {}
with TestClient(api.app) as c:
    out["headers_only"] = [c.get("/v1/onboarding/EMP-001", headers=spoof).status_code,
                           c.post("/v1/runs", headers=spoof, json={"instance": "EMP-001",
                                  "input": {"employee_id": "EMP-001"}}).status_code,
                           c.get("/v1/approvals", headers=spoof).status_code]
    out["bad_tokens"] = [c.get("/v1/onboarding/EMP-001", headers=auth(n)).status_code
                         for n in ("expired", "other_key", "wrong_audience")]
    out["employee_read"] = c.get("/v1/onboarding/EMP-001", headers=auth("employee")).status_code
    out["tenant_from_token"] = c.get("/v1/onboarding/EMP-101", headers=auth("employee", **{"X-Client-Id": "CLIENT-GLOBEX"})).status_code
    sweep = {"as_of": "2026-11-01T00:00:00+04:00"}
    out["sweep"] = [c.post("/v1/onboarding/sessions/check-timeouts", headers=auth("employee", **spoof), json=sweep).status_code,
                    c.post("/v1/onboarding/sessions/check-timeouts", headers=auth("hr_admin"), json=sweep).status_code]
    out["reconciliation"] = [c.get("/v1/onboarding/admin/reconciliation", headers=auth("employee", **spoof)).status_code,
                             c.get("/v1/onboarding/admin/reconciliation", headers=auth("hr_admin")).status_code]
    run = c.post("/v1/runs", headers=auth("hr_admin"), json={"instance": "EMP-001", "input": {"employee_id": "EMP-001"}})
    approval_id = next(a["approval_id"] for a in run.json()["response"]["actions_taken"] if a.get("approval_id"))
    out["decide_as_employee"] = c.post(f"/v1/approvals/{approval_id}/decision", headers=auth("employee", **spoof),
                                       json={"decision": "approved"}).status_code
    api.ctx.connector.notifier.fail_next = 1  # the welcome delivery fails once -> apply_error, retryable
    out["decide_as_hr"] = c.post(f"/v1/approvals/{approval_id}/decision", headers=auth("hr_admin"),
                                 json={"decision": "approved"}).status_code
    out["retry"] = [c.post(f"/v1/approvals/{approval_id}/retry-apply", headers=spoof).status_code,
                    c.post(f"/v1/approvals/{approval_id}/retry-apply", headers=auth("employee", **spoof)).status_code,
                    c.post(f"/v1/approvals/{approval_id}/retry-apply", headers=auth("globex_hr")).status_code,
                    c.post(f"/v1/approvals/{approval_id}/retry-apply", headers=auth("hr_admin")).status_code]
    out["welcomes_sent"] = len(api.ctx.connector.notifier.sent_to("EMP-001", kind="welcome"))
    out["retry_denied_audited"] = len(api.ctx.audit.find("policy.denied"))
print("RESULT " + json.dumps(out))
"""


def test_in_jwt_mode_request_headers_can_never_supply_a_user_role_or_tenant(keys, tmp_path):
    key_file = tmp_path / "idp.pem"
    key_file.write_text(_pem_public(keys["rsa"]), encoding="utf-8")
    now = int(time.time())
    tokens = {
        "hr_admin": token(keys["rsa"]),
        "employee": token(keys["rsa"], sub="EMP-001", roles=["employee"]),
        "globex_hr": token(keys["rsa"], sub="user:globex-hr", client_id="CLIENT-GLOBEX"),
        "expired": token(keys["rsa"], iat=now - 900, exp=now - 600),
        "other_key": token(keys["other"]),
        "wrong_audience": token(keys["rsa"], aud="another-service"),
    }
    env = {k: v for k, v in os.environ.items() if not k.startswith(("IDENTITY_", "HR_AGENT_"))}
    env.update(PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / "src")]), LOG_LEVEL="ERROR", HR_AGENT_ENV="test",
               HR_AGENT_PERSIST="0", IDENTITY_MODE="jwt", IDENTITY_JWT_ISSUER=ISSUER, IDENTITY_JWT_AUDIENCE=AUDIENCE,
               IDENTITY_JWT_PUBLIC_KEY_FILE=str(key_file), TEST_TOKENS=json.dumps(tokens))
    proc = subprocess.run([sys.executable, "-c", API_SCRIPT], capture_output=True, text=True, cwd=ROOT, env=env,
                          timeout=180)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.split("RESULT ", 1)[1])

    assert out["headers_only"] == [401, 401, 401]  # the old trusted headers authenticate nobody
    assert out["bad_tokens"] == [401, 401, 401]
    assert out["employee_read"] == 200
    assert out["tenant_from_token"] == 404  # X-Client-Id cannot move an ACME token into GLOBEX
    assert out["sweep"] == [403, 200]  # X-User-Roles: hr_admin does not make an employee token an HR admin
    assert out["reconciliation"] == [403, 200]
    assert out["decide_as_employee"] == 403
    assert out["decide_as_hr"] == 502  # decided; its delivery failed once and is retryable
    # retry-apply: no token 401, wrong role 403, other tenant 404, the approver role 200
    assert out["retry"] == [401, 403, 404, 200]
    assert out["welcomes_sent"] == 1
