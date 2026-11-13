"""The ONE place UC-02 decides whether test data (synthetic or mock client configuration) may be used. Fails closed.

Rules
-----
* ``UC02_ENV`` must be set explicitly to one of ``development``, ``test``, ``staging`` or ``production``. Missing, empty or unknown is never
  treated as development.
* Synthetic / mock configuration is allowed ONLY when ``UC02_ENV`` is ``development`` or ``test`` AND ``UC02_ALLOW_SYNTHETIC_CONFIG=1``.
* ``production`` (and ``staging``) always deny it. An opt-in combined with a missing, unknown, staging or production environment is an unsafe
  combination and is rejected with a clear error instead of being ignored.
* The configuration reader enforces this itself (``require_synthetic_allowed``), so a caller passing ``allow_synthetic=True`` cannot bypass it, and the
  check is repeated at load time (the environment is read again), so a reader built earlier cannot outlive a change to it.

Limitation: ``UC02_ENV`` describes the PROCESS, not the database it points at. It is a deployment control, not proof of which Supabase project is used.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from onboarding_task_management.errors import MissingConfigurationError

ENV_NAME = "UC02_ENV"
ENV_SYNTHETIC_OPT_IN = "UC02_ALLOW_SYNTHETIC_CONFIG"
KNOWN_ENVIRONMENTS = frozenset({"development", "test", "staging", "production"})
SYNTHETIC_ENVIRONMENTS = frozenset({"development", "test"})


class UnsafeConfigurationError(MissingConfigurationError):
    """The runtime's environment settings are missing, unknown, or combine test data with a non-test environment."""

    code = "POLICY_VIOLATION"


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def environment(environ: Mapping[str, str] | None = None) -> str:
    """The normalised ``UC02_ENV`` value; ``""`` when missing or empty (never a default)."""
    return (_env(environ).get(ENV_NAME) or "").strip().lower()


def _opt_in(env: Mapping[str, str]) -> str:
    value = (env.get(ENV_SYNTHETIC_OPT_IN) or "").strip()
    if value not in ("", "0", "1"):
        raise UnsafeConfigurationError(f"{ENV_SYNTHETIC_OPT_IN} must be 1, 0 or unset", configured="(invalid value)")
    return value


def validate_runtime_environment(environ: Mapping[str, str] | None = None) -> str:
    """Require an explicit, known ``UC02_ENV`` and reject unsafe opt-in combinations. Returns the environment name."""
    env = _env(environ)
    name = environment(env)
    opt_in = _opt_in(env)
    if not name:
        if opt_in == "1":
            raise UnsafeConfigurationError(f"{ENV_SYNTHETIC_OPT_IN}=1 is set but {ENV_NAME} is missing; synthetic configuration is denied "
                                           f"(set {ENV_NAME}=development or test explicitly)")
        raise UnsafeConfigurationError(f"{ENV_NAME} must be set explicitly to one of {', '.join(sorted(KNOWN_ENVIRONMENTS))}; "
                                       "it is never assumed to be development")
    if name not in KNOWN_ENVIRONMENTS:
        raise UnsafeConfigurationError(f"{ENV_NAME} has an unknown value; expected one of {', '.join(sorted(KNOWN_ENVIRONMENTS))}",
                                       configured=name)
    if opt_in == "1" and name not in SYNTHETIC_ENVIRONMENTS:
        raise UnsafeConfigurationError(f"{ENV_SYNTHETIC_OPT_IN}=1 is not allowed when {ENV_NAME}={name}; synthetic and mock configuration "
                                       "is never used outside development and test")
    return name


def synthetic_config_allowed(environ: Mapping[str, str] | None = None) -> bool:
    """True only for an explicit development/test environment that opted in. Never raises; anything doubtful is False."""
    env = _env(environ)
    try:
        return environment(env) in SYNTHETIC_ENVIRONMENTS and _opt_in(env) == "1"
    except UnsafeConfigurationError:
        return False


def require_synthetic_allowed(environ: Mapping[str, str] | None = None, *, purpose: str = "synthetic configuration") -> None:
    """Raise unless the environment explicitly allows test data. Used by the reader itself, so callers cannot bypass it."""
    env = _env(environ)
    validate_runtime_environment(env)  # missing / unknown / production+opt-in raise here with the specific reason
    if not synthetic_config_allowed(env):
        raise UnsafeConfigurationError(f"{purpose} needs {ENV_NAME}=development or test and {ENV_SYNTHETIC_OPT_IN}=1",
                                       environment=environment(env) or "(unset)")


__all__ = ["ENV_NAME", "ENV_SYNTHETIC_OPT_IN", "KNOWN_ENVIRONMENTS", "SYNTHETIC_ENVIRONMENTS", "UnsafeConfigurationError", "environment",
           "require_synthetic_allowed", "synthetic_config_allowed", "validate_runtime_environment"]
