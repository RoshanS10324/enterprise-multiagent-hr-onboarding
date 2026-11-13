"""Identity verification boundary (finding S2).

`X-User-Id`, `X-User-Roles`, and `X-Client-Id` are NOT authentication just because a caller sends
them -- anyone who can reach this service over HTTP can set any value for any of them. This module
is the one place that is allowed to turn raw request headers into a `VerifiedActor`; every protected
route must go through an `IdentityProvider`, never read these headers directly (see api.py).

There is no real identity-provider integration in this project: no JWT validation, no JWKS fetch,
no IdP client exists anywhere in this codebase, and this module does not fabricate one. The only
provider shipped here, `InsecureHeaderIdentityProvider`, reproduces today's header-trust behaviour
and is **disabled by default** -- an application must construct it with `enabled=True` explicitly,
which is itself a visible, auditable decision, not a silent default. See
docs/ONBOARDING_COMMON_ARCHITECTURE.md Part B.5 for the required production boundary: a trusted
gateway/IdP must authenticate the caller and set these headers itself (stripping any client-supplied
copies) before this service ever sees the request. Until that exists, any deployment reachable by
untrusted callers must not enable `InsecureHeaderIdentityProvider` -- there is no code-level
substitute for a real identity provider, and claiming otherwise would misrepresent what this
provides.
"""

from __future__ import annotations

from typing import Mapping, Protocol

from pydantic import BaseModel


class IdentityError(PermissionError):
    """Identity could not be verified. Protected operations must fail closed on this, not fall
    back to an anonymous or permissive actor."""


class VerifiedActor(BaseModel):
    """The output of a real identity check -- distinct from contracts.Actor, which is the
    (possibly unverified) actor shape threaded through agent requests. Call sites that need an
    `Actor` should build one from a `VerifiedActor`, never the reverse."""

    user_id: str
    roles: list[str]
    client_id: str
    auth_method: str  # e.g. "header-trust-dev" (insecure), "jwt" (not implemented in this project)


class IdentityProvider(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> VerifiedActor:
        """Raise IdentityError if identity cannot be verified. Must never return a VerifiedActor
        for a caller it has not actually verified."""
        ...


def require_role(actor: VerifiedActor, role: str, *, action: str) -> None:
    """Fail closed if `actor` does not hold `role`. Every protected action (not just HITL
    decisions, which hr_onboarding_common.platform.hitl already role-checks) must call this or an
    equivalent check -- e.g. signature verification, which upstream had no role gate at all."""
    if role not in actor.roles:
        raise IdentityError(f"{actor.user_id} lacks role '{role}' required for '{action}'")


class InsecureHeaderIdentityProvider:
    """Reproduces the original (and still common, absent a real IdP) behaviour of trusting
    X-User-Id / X-User-Roles / X-Client-Id verbatim. NEVER safe behind anything but a fully
    trusted network with no direct, unauthenticated client access -- it performs no cryptographic
    or out-of-band verification of any kind. Disabled by default: an application must pass
    `enabled=True` explicitly to use it, and should log loudly when it does, so the gap is visible
    at startup rather than silently assumed away.
    """

    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled

    def authenticate(self, headers: Mapping[str, str]) -> VerifiedActor:
        if not self.enabled:
            raise IdentityError(
                "InsecureHeaderIdentityProvider is disabled and no other identity provider is configured; "
                "this deployment cannot verify caller identity (see docs/ONBOARDING_COMMON_ARCHITECTURE.md Part B.5)")
        user_id = headers.get("x-user-id")
        client_id = headers.get("x-client-id")
        if not user_id or not client_id:
            raise IdentityError("missing required identity headers (X-User-Id, X-Client-Id)")
        roles_raw = headers.get("x-user-roles", "")
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
        return VerifiedActor(user_id=user_id, roles=roles, client_id=client_id, auth_method="header-trust-dev")


class RegistryIdentityProvider:
    """Development identities from a SERVER-SIDE registry (config/dev_identities.yaml). The caller names a user
    (X-User-Id) and a tenant (X-Client-Id); the roles -- and which tenants that user may act in -- come from the
    registry, never from the request (X-User-Roles is ignored). An unknown user or a tenant not granted to that user
    is refused. Still a development provider: X-User-Id is not proof of who is calling, so it must only run on a
    loopback/trusted host; a real identity provider replaces it behind the same IdentityProvider interface."""

    auth_method = "dev-registry"

    def __init__(self, users: Mapping[str, Mapping]):
        self.users = {uid: {"label": str(u.get("label") or uid), "roles": [str(r) for r in u.get("roles") or []],
                            "clients": [str(c) for c in u.get("clients") or []]} for uid, u in users.items()}

    @classmethod
    def load(cls, path) -> "RegistryIdentityProvider":
        import yaml
        from pathlib import Path

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(data.get("users") or {})

    def authenticate(self, headers: Mapping[str, str]) -> VerifiedActor:
        user_id, client_id = headers.get("x-user-id"), headers.get("x-client-id")
        if not user_id or not client_id:
            raise IdentityError("missing required identity headers (X-User-Id, X-Client-Id)")
        user = self.users.get(user_id)
        if user is None:
            raise IdentityError("unknown development user")
        if client_id not in user["clients"]:
            raise IdentityError(f"{user_id} may not act for this client")
        return VerifiedActor(user_id=user_id, roles=list(user["roles"]), client_id=client_id,
                             auth_method=self.auth_method)

    def identities(self) -> list[dict]:
        """The registry, for a development identity picker (user ids, labels, roles, clients -- no secrets)."""
        return [{"user_id": uid, **u} for uid, u in self.users.items()]
