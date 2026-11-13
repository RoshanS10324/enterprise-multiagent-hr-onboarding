"""Serve the onboarding orchestrator API for the AI Onboarding Control Center (onboarding-ui-prototype).

    python scripts/serve_orchestrator.py --dev-identity                   real UC agents (default)
    python scripts/serve_orchestrator.py --dev-identity --mode demo       synthetic UC stand-ins, clearly
                                                                          labelled data_origin=synthetic

Modes (the orchestrator graph, routing, gates, UC03 and the state store are real in both):
  real   UC01/UC04/UC05 through their worker processes (UC0X_WORKER_PYTHON, default the environments
         under %LOCALAPPDATA%/hr-onboarding-envs), UC02 and UC03 in-process
         (scripts/run_workflow_demo.build_real_agent). Known blockers of the real UCs are reported per stage
         (docs/WORKFLOW_IMPLEMENTATION_REPORT.md section 5).
  demo   scripts/run_workflow_demo.build_synthetic_agent: stand-ins with each UC's output shape for UC01,
         UC02, UC04 and UC05; real UC03. Every result says data_origin=synthetic.

Environment (HR-Onboarding-Common/.env or the process environment; secrets stay on this backend):
  ORCHESTRATOR_STATE_STORE   memory | sqlite | supabase (default sqlite under --persist-dir). supabase needs
                             ORCHESTRATOR_SUPABASE_URL / ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY (or the plain
                             SUPABASE_* names) and an onboarding_cases row per case (written by UC01).
  UC02_STORE_MODE            UC02's store (adapters/uc02_runtime.py): supabase with UC02_ENV and
                             UC02_WORKER_ENV__SUPABASE_URL / UC02_WORKER_ENV__SUPABASE_SERVICE_ROLE_KEY. In real
                             mode a Supabase case-state store requires UC02 in Supabase mode too: the server refuses
                             to start a durable workflow whose task plans would live in process memory.
  DOCUMENT_STORAGE_BUCKET    the Supabase Storage bucket for document uploads (e.g. Doc_Collection).
                             Unset: the upload route answers 503 (no fallback).
  DOCUMENT_MAX_BYTES         upload size limit (default 10 MiB)
  ORCHESTRATOR_CORS_ORIGINS  comma-separated browser origins (default the Vite dev server on :5173)

Identity: no identity provider is integrated in this project (platform/identity.py). --dev-identity
enables InsecureHeaderIdentityProvider (trusts X-User-Id / X-Client-Id / X-User-Roles). Without the flag
the server refuses to start. Never expose a --dev-identity server beyond localhost.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import run_workflow_demo as demo  # noqa: E402  (reuses its agent builders; no duplicate wiring)

from hr_onboarding_common.config import load_environment  # noqa: E402
from hr_onboarding_common.orchestration.adapters import uc02_runtime  # noqa: E402
from hr_onboarding_common.orchestration.api import create_orchestrator_app  # noqa: E402
from hr_onboarding_common.orchestration.approval_requests import SupabaseApprovalRequests  # noqa: E402
from hr_onboarding_common.orchestration.documents import build_document_service  # noqa: E402
from hr_onboarding_common.orchestration.tenant_config import TenantConfig  # noqa: E402
from hr_onboarding_common.platform.identity import (InsecureHeaderIdentityProvider,  # noqa: E402
                                                    RegistryIdentityProvider)

DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


def uc01_store_mode(environ: dict[str, str] | None = None) -> str:
    """`supabase` when the UC01 worker will run in its Supabase mode (it then stores its cases in onboarding_cases),
    else `memory`. The worker receives UC01_WORKER_ENV__NAME as NAME, and a bare SUPABASE_URL otherwise
    (workers/config.child_environment); UC01 itself switches on SUPABASE_URL."""
    env = os.environ if environ is None else environ
    url = (env.get("UC01_WORKER_ENV__SUPABASE_URL") or env.get("SUPABASE_URL") or "").strip()
    return "supabase" if url else "memory"


def store_modes(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Where each part keeps its records, as the processes will see it (worker-scoped settings first). Values only --
    never a URL or a key."""
    env = os.environ if environ is None else environ

    def setting(name: str, uc: str | None = None) -> str:
        return ((env.get(f"{uc}_WORKER_ENV__{name}") if uc else None) or env.get(name) or "").strip().lower()

    state = setting("ORCHESTRATOR_STATE_STORE")
    return {
        "uc03": setting("UC03_STORE_MODE") or "memory",
        "uc04_state": setting("UC04_STATE_MODE", "UC04") or "memory",
        "uc04_config": setting("UC04_CONFIG_SOURCE") or "file",
        "uc05": setting("UC05_DATABASE_MODE", "UC05") or "local",
        "joining_day_jobs": setting("ORCHESTRATOR_JOINING_DAY_STORE") or ("supabase" if state == "supabase" else "sqlite"),
    }


def build_app(*, mode: str, persist_dir: Path | None, dev_identity: bool):
    if not dev_identity:
        raise SystemExit("refusing to start: no identity provider is configured. Pass --dev-identity for local "
                         "development (trusts identity headers; localhost only).")
    load_environment()
    if persist_dir is not None:
        os.environ.setdefault("ORCHESTRATOR_STATE_STORE", "sqlite")
        # UC05 keeps its cases, sessions, approvals and checkpoints next to the orchestrator's own state. Without it
        # every restart wipes UC05 while the orchestrator still remembers what UC05 did (UNCERTAIN_OUTCOME).
        os.environ.setdefault("UC05_WORKER_DATA_DIR", str((persist_dir / "uc05").resolve()))
    state_store_mode = (os.environ.get("ORCHESTRATOR_STATE_STORE") or "").strip().lower()
    uc02_store = uc02_runtime.configured_store_mode()
    if mode == "real" and state_store_mode == "supabase" and uc02_store != "supabase":
        raise SystemExit(f"refusing to start: ORCHESTRATOR_STATE_STORE=supabase but UC02_STORE_MODE={uc02_store}. UC02's "
                         "task plans would live in process memory and be lost on restart while the workflow state says "
                         "they exist. Set UC02_WORKER_ENV__UC02_STORE_MODE=supabase (with UC02_WORKER_ENV__UC02_ENV and "
                         "UC02_WORKER_ENV__SUPABASE_URL / UC02_WORKER_ENV__SUPABASE_SERVICE_ROLE_KEY).")
    if mode == "real" and uc02_store == "supabase" and state_store_mode != "supabase":
        print(f"warning: UC02 plans in Supabase but the case state is kept in {state_store_mode or 'memory'}; set "
              "ORCHESTRATOR_STATE_STORE=supabase to keep the workflow state in case_orchestration_state")
    uc01_store = uc01_store_mode()
    if mode == "real" and state_store_mode == "supabase" and uc01_store != "supabase":
        print("warning: ORCHESTRATOR_STATE_STORE=supabase but UC01 runs in memory (no UC01_WORKER_ENV__SUPABASE_URL): "
              "UC01 does not store its cases in onboarding_cases, so every new case stops after the form approval with "
              "CASE_NOT_IN_DATABASE. Enable UC01's Supabase mode (UC01_WORKER_ENV__SUPABASE_URL and "
              "UC01_WORKER_ENV__UC01_FIELD_ENCRYPTION_KEY) or set ORCHESTRATOR_STATE_STORE=sqlite.")
    modes = store_modes()
    if mode == "real":
        print("stores: " + ", ".join(f"{k}={v}" for k, v in modes.items()))
        local = [k for k, v in modes.items() if v not in ("supabase",)]
        if state_store_mode == "supabase" and local:
            print(f"warning: the case state is in Supabase but {', '.join(local)} still keep their records outside it "
                  f"(docs/SUPABASE_RUNTIME_ARCHITECTURE.md lists the settings)")
    agent = demo.build_real_agent(persist_dir) if mode == "real" else demo.build_synthetic_agent(persist_dir)
    if mode == "demo":  # build_deps loads the tenant settings in real mode; the demo builder does not
        tenants = os.environ.get("ORCHESTRATOR_TENANT_CONFIG") or str(demo.COMMON / "config" / "tenants.yaml")
        agent.deps.tenants = TenantConfig.load(tenants)
    documents = build_document_service(os.environ, uc03_repo_root=demo.UC03_ROOT)
    # The orchestrator's approval requests (incl. the internal onboarding form's) -> Supabase approval_requests.
    agent.deps.approval_mirror = SupabaseApprovalRequests.from_env(os.environ)
    store = type(agent.deps.case_states).__name__
    origins = [o.strip() for o in (os.environ.get("ORCHESTRATOR_CORS_ORIGINS") or DEFAULT_ORIGINS).split(",")
               if o.strip()]
    # Development identity: the server-side registry (roles resolved here, never taken from the browser) when
    # config/dev_identities.yaml exists; header trust otherwise.
    registry = Path(os.environ.get("ORCHESTRATOR_DEV_IDENTITIES") or demo.COMMON / "config" / "dev_identities.yaml")
    provider = RegistryIdentityProvider.load(registry) if registry.exists() else InsecureHeaderIdentityProvider(enabled=True)
    app = create_orchestrator_app(
        agent=agent, identity_provider=provider, documents=documents,
        cors_origins=origins,
        runtime_info={"mode": mode, "data_origin": "synthetic" if mode == "demo" else "live_agents",
                      "state_store": store,
                      # Where UC02 keeps its tasks, dependencies and plan revisions (never its credentials).
                      "uc02_store": uc02_store if mode == "real" else "synthetic stand-in",
                      # Whether UC01 stores its cases in onboarding_cases (a Supabase case state needs that row).
                      "uc01_store": uc01_store if mode == "real" else "synthetic stand-in",
                      # UC03 / UC04 / UC05 / joining-day jobs: where each keeps its records (no credentials).
                      "stores": modes if mode == "real" else {"all": "synthetic stand-in"},
                      "identity": "dev-registry" if isinstance(provider, RegistryIdentityProvider) else "dev-header-trust",
                      "approval_requests": "supabase" if agent.deps.approval_mirror is not None else "not configured",
                      "form_approval_role": agent.policy.gate("onboarding_form_approval").approver_role})
    demo._declare_dev_identity_headers(app)
    return app, agent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("real", "demo"), default="real")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--persist-dir", type=Path, default=demo.COMMON / ".dact" / "orchestrator",
                        help="durable case states, checkpoints and UC03 registry (a restart resumes the same cases)")
    parser.add_argument("--dev-identity", action="store_true",
                        help="trust X-User-Id / X-Client-Id / X-User-Roles (local development only)")
    args = parser.parse_args(argv)
    if args.dev_identity and args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("--dev-identity is only allowed on a loopback host")

    app, agent = build_app(mode=args.mode, persist_dir=args.persist_dir, dev_identity=args.dev_identity)
    print(f"orchestrator mode={args.mode} store={type(agent.deps.case_states).__name__} "
          f"http://{args.host}:{args.port}  (development identity headers are trusted)")
    try:
        import uvicorn

        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        if args.mode == "real":
            agent.deps.worker_pool.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
