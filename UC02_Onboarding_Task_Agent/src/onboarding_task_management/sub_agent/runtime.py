"""Build the UC-02 service for the storage mode the environment selects.

``UC02_STORE_MODE`` unset or ``memory``  -> no durable store (the default; what every
                                            existing unit test uses).
``UC02_STORE_MODE=sqlite``               -> local SQLite database at ``UC02_SQLITE_PATH``
                                            (default ``local_db/uc02_dev.sqlite3``).

If SQLite is requested and cannot be opened this raises ``PersistenceError``. It never
falls back to memory. The client configuration is still the synthetic in-process
registry; no approved real client configuration exists yet.
"""

from __future__ import annotations

from collections.abc import Mapping

from onboarding_task_management.contracts import SourceUseCase
from onboarding_task_management.persistence import store_from_env
from onboarding_task_management.sub_agent.checklist import ConfigRegistry
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import build_registry


ENV_STORE_MODE = "UC02_STORE_MODE"
ENV_ALLOW_SYNTHETIC = "UC02_ALLOW_SYNTHETIC_CONFIG"


def require_supabase_runtime(environ: Mapping[str, str] | None = None) -> None:
    """Refuse to start an agent workflow runtime on anything but Supabase.

    Memory and SQLite exist for isolated unit tests that inject them explicitly. Every runtime entry point (the API, the
    flow factory, the demos) calls this first, so a missing or wrong ``UC02_STORE_MODE`` is a clear startup error and never
    a silent switch to a store that loses the workflow on restart.
    """
    import os

    from onboarding_task_management.errors import MissingConfigurationError

    from onboarding_task_management.config_safety import validate_runtime_environment

    env = os.environ if environ is None else environ
    mode = (env.get(ENV_STORE_MODE) or env.get("PERSISTENCE_BACKEND") or "").strip().lower()
    if mode != "supabase":
        raise MissingConfigurationError(
            "the UC-02 agent runtime requires UC02_STORE_MODE=supabase; memory and sqlite are for isolated unit tests only",
            configured=mode or "(unset)")
    validate_runtime_environment(env)  # UC02_ENV explicit and known; no test-data opt-in outside development/test


def build_runtime_service(environ: Mapping[str, str] | None = None, *, source_principals=None,
                          require_plan_approval: bool = True, rest=None) -> OnboardingService:
    """The Supabase-only service every workflow runtime uses. Raises (never falls back) on any configuration problem."""
    import os

    env = os.environ if environ is None else environ
    require_supabase_runtime(env)
    service = build_service(env, source_principals=source_principals, require_plan_approval=require_plan_approval,
                            rest=rest)
    if service.store is None or not getattr(service.store.client, "uses_case_ids", False):
        raise RuntimeError("supabase mode did not produce a Supabase-backed store")  # defensive: never run on anything else
    return service


def build_service(
    environ: Mapping[str, str] | None = None,
    *,
    registry: ConfigRegistry | None = None,
    source_principals: Mapping[SourceUseCase, frozenset[str]] | None = None,
    require_plan_approval: bool = True,
    rest=None,
) -> OnboardingService:
    """An :class:`OnboardingService` backed by the store ``environ`` selects.

    In Supabase mode, with no registry given, the client configuration and working calendar are read
    from the approved Supabase records (a missing, draft or mock configuration is a configuration gap),
    never from the synthetic registry. Memory and SQLite modes keep the synthetic registry for local use.
    """
    store = store_from_env(environ, rest=rest)
    calendars = None
    require_calendar = False
    if registry is None:
        if store is not None and getattr(store.client, "uses_case_ids", False):
            from onboarding_task_management.supabase_config import (
                SupabaseCalendars,
                SupabaseConfigRegistry,
                SupabaseConfigSource,
            )

            import os as _os

            from onboarding_task_management.config_safety import synthetic_config_allowed, validate_runtime_environment

            env_name = (environ if environ is not None else _os.environ)
            validate_runtime_environment(env_name)
            source = SupabaseConfigSource(store.client.rest, allow_synthetic=synthetic_config_allowed(env_name), environ=env_name)
            registry = SupabaseConfigRegistry(source=source)
            calendars, require_calendar = SupabaseCalendars(source), True
        else:
            registry = build_registry()
    return OnboardingService(
        registry,
        store=store,
        calendars=calendars,
        require_client_calendar=require_calendar,
        source_principals=source_principals,
        require_plan_approval=require_plan_approval,
    )


ENV_CALLBACK_SECRET = "UC02_APPROVAL_CALLBACK_SECRET"
ENV_CALLBACK_URL = "UC02_APPROVAL_CALLBACK_URL"
ENV_COORDINATOR_ID = "UC02_COORDINATOR_ID"
#: Per-channel HMAC secrets for the signed inbound events.
ENV_CHANNEL_SECRETS = {"events": "UC02_EVENT_SECRET", "documents": "UC02_DOCUMENTS_SECRET",
                       "uc04": "UC02_UC04_STATUS_SECRET", "uc05": "UC02_UC05_STATUS_SECRET"}
#: Outbound trigger endpoints, supplied by the UC-04 / UC-05 teams. None is assumed.
ENV_TRIGGERS = {"UC-04": ("UC02_UC04_TRIGGER_URL", "UC02_UC04_TRIGGER_SECRET"),
                "UC-05": ("UC02_UC05_TRIGGER_URL", "UC02_UC05_TRIGGER_SECRET")}


def build_flow(environ: Mapping[str, str] | None = None, *, service: OnboardingService | None = None,
               employee_source=None, connector=None, rest=None, downstream_adapters=None):
    """The runtime flow, configured from the environment. Raises if the approval secret is missing.

    * ``service``: share an existing service (the API does, so callbacks see the plans it submitted); otherwise one is built
      for the selected store, registering the signed channels' service identities.
    * ``employee_source``: defaults to the read-only Supabase source in Supabase mode, and to none otherwise (employee
      events are then refused with a clear error rather than guessed).
    * ``connector``: defaults to ``build_outlook_connector`` (mock unless ``UC02_OUTLOOK_CONNECTOR=graph``; never a silent
      fall-back from graph).
    * downstream adapters exist only for use cases whose trigger URL and secret are configured.
    """
    import os

    from onboarding_task_management.contracts import Actor, Role
    from onboarding_task_management.errors import MissingConfigurationError
    from onboarding_task_management.sub_agent.connectors.outlook.factory import build_outlook_connector
    from onboarding_task_management.sub_agent.downstream import HttpTriggerAdapter
    from onboarding_task_management.sub_agent.flow_runtime import RuntimeFlow, runtime_principals

    env = os.environ if environ is None else environ
    secret = env.get(ENV_CALLBACK_SECRET, "")
    if not secret:
        raise MissingConfigurationError(f"no approval callback secret is configured ({ENV_CALLBACK_SECRET})")
    service = service or build_runtime_service(env, source_principals=runtime_principals(), rest=rest)
    if employee_source is None and service.store is not None and getattr(service.store.client, "uses_case_ids", False):
        from onboarding_task_management.sub_agent.flow import SupabaseEmployeeSource
        from onboarding_task_management.supabase_rest import SupabaseGateway

        employee_source = SupabaseEmployeeSource(SupabaseGateway(service.store.client.rest))
    adapters = {}
    for use_case, (url_var, secret_var) in ENV_TRIGGERS.items():
        if env.get(url_var) and env.get(secret_var):
            adapters[use_case] = HttpTriggerAdapter(use_case, env[url_var], env[secret_var].encode("utf-8"))
    return RuntimeFlow(
        service, employee_source, coordinator=Actor(actor_id=env.get(ENV_COORDINATOR_ID, "uc02-coordinator"),
                                                    role=Role.HR_OPS, client_id=""),
        callback_secret=secret.encode("utf-8"), approval_callback_url=env.get(ENV_CALLBACK_URL) or None,
        connector=connector if connector is not None else build_outlook_connector(env),
        downstream_adapters=adapters if downstream_adapters is None else downstream_adapters,
        channel_secrets={ch: env.get(var, "").encode("utf-8") for ch, var in ENV_CHANNEL_SECRETS.items()})


__all__ = ["build_flow", "build_service"]
