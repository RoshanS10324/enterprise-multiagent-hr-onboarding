"""Caller identity for the onboarding API (finding S2).

The API never trusts X-User-Id / X-User-Roles / X-Client-Id in production. Identity, roles and
tenant come from a verified credential:

  IDENTITY_MODE=jwt (the default)
      `Authorization: Bearer <JWT>` issued by the organisation's identity provider, or by a trusted
      gateway that authenticated the caller and minted a short-lived token. Verified locally:
        - signature, with an asymmetric algorithm only (RS256/RS384/RS512/ES256/ES384 by default --
          `none` and HMAC algorithms are refused, which rules out alg-confusion attacks), against
          IDENTITY_JWT_PUBLIC_KEY(_FILE) or keys fetched from IDENTITY_JWKS_URL;
        - `iss` == IDENTITY_JWT_ISSUER and `aud` == IDENTITY_JWT_AUDIENCE;
        - `exp`, `iat` and `sub` present, `exp`/`nbf` honoured (IDENTITY_JWT_LEEWAY_S of clock skew),
          and the token no older than IDENTITY_JWT_MAX_AGE_S;
        - a non-empty tenant claim (IDENTITY_JWT_TENANT_CLAIM, default "client_id") and a list of
          roles (IDENTITY_JWT_ROLES_CLAIM, default "roles").
      Any request header claiming a user, role or tenant is ignored.

  IDENTITY_MODE=insecure-dev-headers
      The old header trust (InsecureHeaderIdentityProvider), for local development and tests only.
      Refused at startup unless HR_AGENT_ENV is "development" or "test" -- HR_AGENT_ENV defaults to
      "production", so it cannot be switched on by accident.

What is NOT verified here: no real identity provider has been configured or tested against. The
JWKS path uses PyJWT's PyJWKClient and has only been exercised with locally generated keys; the
issuer/audience/claim names must be agreed with the organisation's IdP before use.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import jwt

from hr_onboarding_common.platform.identity import (IdentityError, IdentityProvider,
                                                     InsecureHeaderIdentityProvider, VerifiedActor)

ASYMMETRIC_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512",
                                   "EdDSA"})
DEV_ENVIRONMENTS = frozenset({"development", "test"})


class IdentityConfigurationError(RuntimeError):
    """The identity configuration is missing, contradictory, or unsafe. Raised at startup."""


@dataclass(frozen=True)
class JwtSettings:
    issuer: str
    audience: str
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    public_key_pem: str | None = None
    jwks_url: str | None = None
    roles_claim: str = "roles"
    tenant_claim: str = "client_id"
    leeway_s: int = 30
    max_age_s: int = 3600

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience:
            raise IdentityConfigurationError("IDENTITY_JWT_ISSUER and IDENTITY_JWT_AUDIENCE are required")
        unsafe = set(self.algorithms) - ASYMMETRIC_ALGORITHMS
        if unsafe or not self.algorithms:
            raise IdentityConfigurationError(f"only asymmetric JWT algorithms are allowed, got {sorted(unsafe)}")
        if bool(self.public_key_pem) == bool(self.jwks_url):
            raise IdentityConfigurationError("set exactly one of IDENTITY_JWT_PUBLIC_KEY(_FILE) or IDENTITY_JWKS_URL")


class JwtIdentityProvider:
    """IdentityProvider that accepts only a verified bearer JWT."""

    def __init__(self, settings: JwtSettings):
        self.settings = settings
        self._jwks = jwt.PyJWKClient(settings.jwks_url, cache_keys=True) if settings.jwks_url else None

    def _key(self, token: str):
        if self._jwks is not None:
            try:
                return self._jwks.get_signing_key_from_jwt(token).key
            except jwt.PyJWKClientError as e:
                raise IdentityError("signing key could not be resolved") from e
        return self.settings.public_key_pem

    def authenticate(self, headers: Mapping[str, str]) -> VerifiedActor:
        scheme, _, token = (headers.get("authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise IdentityError("missing bearer token")
        s = self.settings
        try:
            claims = jwt.decode(token.strip(), self._key(token.strip()), algorithms=list(s.algorithms),
                                audience=s.audience, issuer=s.issuer, leeway=s.leeway_s,
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.ExpiredSignatureError:
            raise IdentityError("token expired") from None
        except jwt.InvalidTokenError as e:
            raise IdentityError(f"invalid token ({type(e).__name__})") from None
        if not isinstance(claims["iat"], (int, float)) or time.time() - claims["iat"] > s.max_age_s + s.leeway_s:
            raise IdentityError("token too old")
        tenant, roles, subject = claims.get(s.tenant_claim), claims.get(s.roles_claim, []), claims["sub"]
        if not isinstance(tenant, str) or not tenant.strip():
            raise IdentityError(f"token has no '{s.tenant_claim}' claim")
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            raise IdentityError(f"token '{s.roles_claim}' claim must be a list of strings")
        if not isinstance(subject, str) or not subject.strip():
            raise IdentityError("token has no subject")
        return VerifiedActor(user_id=subject, roles=roles, client_id=tenant, auth_method="jwt")


def build_identity_provider(environ: Mapping[str, str] | None = None) -> tuple[IdentityProvider, str]:
    """Returns (provider, mode) from the environment, or raises IdentityConfigurationError."""
    env = os.environ if environ is None else environ
    if "HR_AGENT_ALLOW_INSECURE_HEADER_IDENTITY" in env:
        raise IdentityConfigurationError("HR_AGENT_ALLOW_INSECURE_HEADER_IDENTITY was replaced by IDENTITY_MODE; "
                                         "see .env.example")
    mode = env.get("IDENTITY_MODE", "jwt").strip().lower()
    dact_env = env.get("HR_AGENT_ENV", "production").strip().lower()
    if mode == "insecure-dev-headers":
        if dact_env not in DEV_ENVIRONMENTS:
            raise IdentityConfigurationError(f"IDENTITY_MODE=insecure-dev-headers is refused when HR_AGENT_ENV={dact_env!r}; "
                                             f"it is only allowed for HR_AGENT_ENV in {sorted(DEV_ENVIRONMENTS)}")
        return InsecureHeaderIdentityProvider(enabled=True), mode
    if mode != "jwt":
        raise IdentityConfigurationError(f"unknown IDENTITY_MODE {mode!r} (expected 'jwt' or 'insecure-dev-headers')")
    key = env.get("IDENTITY_JWT_PUBLIC_KEY") or None
    if env.get("IDENTITY_JWT_PUBLIC_KEY_FILE"):
        key = Path(env["IDENTITY_JWT_PUBLIC_KEY_FILE"]).read_text(encoding="utf-8")
    algorithms = tuple(a.strip() for a in env.get("IDENTITY_JWT_ALGORITHMS", "RS256,ES256").split(",") if a.strip())
    settings = JwtSettings(issuer=env.get("IDENTITY_JWT_ISSUER", ""), audience=env.get("IDENTITY_JWT_AUDIENCE", ""),
                           algorithms=algorithms, public_key_pem=key, jwks_url=env.get("IDENTITY_JWKS_URL") or None,
                           roles_claim=env.get("IDENTITY_JWT_ROLES_CLAIM", "roles"),
                           tenant_claim=env.get("IDENTITY_JWT_TENANT_CLAIM", "client_id"),
                           leeway_s=int(env.get("IDENTITY_JWT_LEEWAY_S", "30")),
                           max_age_s=int(env.get("IDENTITY_JWT_MAX_AGE_S", "3600")))
    return JwtIdentityProvider(settings), mode
