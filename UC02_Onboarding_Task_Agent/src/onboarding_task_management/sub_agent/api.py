"""FastAPI surface for the UC-02 sub-agent.

The API is a transport, nothing more. It validates the request, hands it to
:class:`~onboarding_task_management.sub_agent.UC02SubAgent`, and shapes the reply. It
contains no onboarding rule of its own, and it has no path that returns a
success without the real workflow having run.

Endpoints
---------

===========================================  ==============================
``GET  /health``                             liveness plus an honest
                                             statement of what is wired
``GET  /v1/fixtures``                        synthetic fixtures accepted by
                                             the workflow endpoint
``POST /v1/onboarding/workflow``             run the UC-02 workflow
``POST /v1/approvals/callback``              receive an HMAC-SIGNED HR approval
                                             decision (503 until a secret is set)
===========================================  ==============================

Status codes
------------

``200`` ran · ``400`` bad request (unknown fixture, both/neither inputs) ·
``403`` unauthorised or cross-client · ``409`` state conflict ·
``422`` schema validation (FastAPI) · ``500`` unexpected.

Internal exception text is never returned. Domain errors carry a stable
``code`` from ``onboarding_task_management.errors``; anything unrecognised is
logged server-side and answered with a generic message.

Run it:

    uvicorn onboarding_task_management.sub_agent.api:app --port 8000
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

# A uvicorn worker, or this module run as a script, never loads the
# repository conftest. Put `src/` on the path so the package is importable
# without an install step.
_ROOT = Path(__file__).resolve().parents[3]
_SRC = str(_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from onboarding_task_management import envfile
from onboarding_task_management.contracts import Actor, Role
from onboarding_task_management.errors import (
    DataIsolationError,
    DuplicateOnboardingError,
    InvalidStateError,
    MissingConfigurationError,
    OnboardingError,
    PreconditionError,
    UnauthorizedActionError,
)
from onboarding_task_management.sub_agent.adapter import (
    DEFAULT_AS_OF,
    UC02SubAgent,
    WorkflowError,
    WorkflowRequest,
    available_fixtures,
)
from onboarding_task_management.sub_agent.flow import (
    ApprovalCallback,
    DocumentUpdate,
    DownstreamStatusEvent,
    EmployeeCreatedEvent,
    OnboardingFlow,
)
from onboarding_task_management.sub_agent.flow_runtime import MessageDecision, runtime_principals
from onboarding_task_management.sub_agent.runtime import (
    build_flow,
    build_runtime_service,
    build_service,
    require_supabase_runtime,
)
from onboarding_task_management.sub_agent.fakes import (
    FAKE_BEHAVIOURS,
    fake_llm_fingerprint,
)

log = logging.getLogger("uc02.api")

API_VERSION = "0.1.0"

#: Domain error -> HTTP status. Anything absent becomes 500.
STATUS_BY_ERROR: dict[type[OnboardingError], int] = {
    WorkflowError: 400,
    MissingConfigurationError: 400,
    PreconditionError: 422,
    UnauthorizedActionError: 403,
    DataIsolationError: 403,
    DuplicateOnboardingError: 409,
    InvalidStateError: 409,
}

app = FastAPI(
    title="UC-02 Onboarding Task Management — sub-agent",
    version=API_VERSION,
    description=(
        "Deterministic onboarding workflow with a FAKE LLM. No real model, "
        "no database and no mail server are contacted by this service."
    ),
)

#: One sub-agent for the process, so repeated requests observe the same records and duplicate detection is visible
#: through the API. It is built on first use and ONLY on Supabase: with ``UC02_STORE_MODE`` unset (or memory / sqlite) the
#: request fails with a clear 503 instead of running on a store that loses the workflow on restart. Tests inject an explicit
#: in-memory one through :func:`reset_sub_agent`.
_sub_agent: UC02SubAgent | None = None


def get_sub_agent() -> UC02SubAgent:
    global _sub_agent
    if _sub_agent is None:
        envfile.load_environment()  # .env then .env.local, never overriding the process environment
        try:
            _sub_agent = UC02SubAgent(service=build_runtime_service(os.environ, source_principals=runtime_principals()))
        except OnboardingError as exc:
            raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from None
        except Exception as exc:  # unreachable Supabase, bad credentials, incomplete schema: fail, never fall back
            raise HTTPException(status_code=503, detail={"code": "RUNTIME_NOT_AVAILABLE",
                                                          "message": f"{type(exc).__name__}: {str(exc)[:200]}"}) from None
    return _sub_agent


def reset_sub_agent() -> UC02SubAgent:
    """Install a fresh IN-MEMORY sub-agent. For isolated unit tests only; never called by a route."""
    global _sub_agent, _flow
    _sub_agent = UC02SubAgent(service=build_service(source_principals=runtime_principals()))
    _flow = None
    return _sub_agent


#: The approval flow shares the sub-agent's service, so a callback sees the plan that was submitted. It exists only when a
#: callback secret is configured: with none, the route refuses (503) rather than accept an unauthenticated decision.
_flow: OnboardingFlow | None = None
ENV_CALLBACK_SECRET = "UC02_APPROVAL_CALLBACK_SECRET"
ENV_COORDINATOR_ID = "UC02_COORDINATOR_ID"


def configure_flow(flow: OnboardingFlow | None) -> None:
    """Install (or clear) the flow the callback route uses. For deployment wiring and tests."""
    global _flow
    _flow = flow


def get_flow() -> OnboardingFlow:
    global _flow
    if _flow is None:
        envfile.load_environment()
        if not os.environ.get(ENV_CALLBACK_SECRET, ""):
            raise HTTPException(status_code=503, detail={
                "code": "APPROVAL_CALLBACK_NOT_CONFIGURED",
                "message": f"no approval callback secret is configured ({ENV_CALLBACK_SECRET}); decisions are refused"})
        try:
            _flow = build_flow(os.environ, service=get_sub_agent().service)
        except OnboardingError as exc:
            raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from None
    return _flow


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    version: str
    llm: dict[str, Any]
    outlook: dict[str, Any]
    database: dict[str, Any]
    fixtures_available: int


class WorkflowBody(BaseModel):
    """Inline trigger or a named synthetic fixture — exactly one."""

    trigger: dict[str, Any] | None = Field(
        default=None, description="Inline OnboardingTrigger payload (synthetic only)."
    )
    fixture: str | None = Field(
        default=None, description="Name of an approved fixture under data/."
    )
    as_of: date = Field(
        default=DEFAULT_AS_OF,
        description="Evaluation date. Explicit, so results are reproducible.",
    )
    actor_id: str = "U-HR-1"
    actor_role: Role = Role.HR_OPS
    llm_behaviour: str = Field(
        default="scripted", description=f"One of {list(FAKE_BEHAVIOURS)}."
    )
    prepare_reminders: bool = False
    outlook_approval_id: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _database_status() -> dict[str, Any]:
    """What storage is actually in use. Never claims more than is wired."""
    if _sub_agent is None:
        try:
            require_supabase_runtime(os.environ)
        except OnboardingError as exc:
            return {"configured": False, "engine": None, "note": f"NOT RUNNING: {exc.message}"}
    store = get_sub_agent().service.store
    if store is None:
        return {
            "configured": False,
            "engine": None,
            "note": "in-memory only; no durable persistence across restarts",
        }
    engine = "supabase" if getattr(store.client, "uses_case_ids", False) else type(store.client).__name__
    return {
        "configured": True,
        "engine": engine,
        "note": ("Supabase PostgreSQL (shared tables)" if engine == "supabase"
                 else "NOT the runtime backend: an injected test store"),
    }


@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health() -> HealthResponse:
    """Liveness, and a factual statement of what is actually wired.

    Deliberately reports what is *absent* as well as what is present, so a
    reader cannot infer a real LLM or database from a 200.
    """
    return HealthResponse(
        status="ok",
        version=API_VERSION,
        llm=fake_llm_fingerprint(),
        outlook={
            "connector": "mock",
            "real_email_sent": False,
            "requires_credentials": False,
        },
        database=_database_status(),
        fixtures_available=len(available_fixtures()),
    )


@app.get("/v1/fixtures", tags=["workflow"])
def fixtures() -> dict[str, Any]:
    """Synthetic fixtures the workflow endpoint will accept by name."""
    names = available_fixtures()
    return {"count": len(names), "fixtures": names}


_CALLBACK_HTTP = {"approved": 200, "rejected": 200, "changes_requested": 200, "replayed": 200,
                  "invalid_signature": 401, "unauthorized": 403, "unknown_request": 404, "case_mismatch": 409,
                  "stale_version": 409, "not_pending": 409, "expired": 409}


@app.post("/v1/approvals/callback", tags=["approval"])
def approval_callback(body: ApprovalCallback) -> dict[str, Any]:
    """Receive an HR decision relayed by the approval connector.

    The body must carry an HMAC signature over its fields (including the client id). The signature, freshness, replay,
    plan version, request state, expiry and the approver's authority are all checked in
    :meth:`OnboardingFlow.process_signed_callback`; this route only maps the outcome to a status code. A valid approval
    starts team coordination (assignments and the document request are prepared; nothing is sent).
    """
    outcome = get_flow().process_signed_callback(body, datetime.now(UTC))
    status = _CALLBACK_HTTP.get(outcome.status, 400)
    payload = {"status": outcome.status, "detail": outcome.detail, "onboarding_id": outcome.onboarding_id,
               "plan_version": outcome.plan_version, "new_request_id": outcome.new_request_id}
    if status >= 400:
        raise HTTPException(status_code=status, detail=payload)
    return payload


_RECEIVE_HTTP = {"accepted": 200, "duplicate": 200, "applied": 200, "sent": 200, "replayed": 200,
                 "invalid_signature": 401, "not_configured": 503, "connector_not_configured": 503, "unauthorized": 403,
                 "unknown_request": 404, "expired": 409, "held": 409, "rejected": 422, "configuration_gap": 422, "failed": 502}


def _respond(outcome) -> dict[str, Any]:
    status = _RECEIVE_HTTP.get(outcome.status, 400)
    payload = {"status": outcome.status, "detail": outcome.detail, **outcome.data}
    if status >= 400:
        raise HTTPException(status_code=status, detail=payload)
    return payload


@app.post("/v1/events/employee-created", tags=["events"])
def employee_created(body: EmployeeCreatedEvent) -> dict[str, Any]:
    """UC-01 employee-created event (HMAC-signed). Starts the UC-02 flow; a repeat for the same employee is a duplicate."""
    return _respond(get_flow().receive_employee_event(body, date.today()))


@app.post("/v1/documents/status", tags=["documents"])
def document_status(body: DocumentUpdate) -> dict[str, Any]:
    """A document's reported status (HMAC-signed by the document collection process)."""
    return _respond(get_flow().receive_document_update(body))


@app.post("/v1/downstream/status", tags=["downstream"])
def downstream_status(body: DownstreamStatusEvent) -> dict[str, Any]:
    """UC-04 / UC-05 status event (HMAC-signed with that use case's secret)."""
    return _respond(get_flow().receive_downstream_status(body))


@app.post("/v1/messages/decision", tags=["approval"])
def message_decision(body: MessageDecision) -> dict[str, Any]:
    """An HR lead's signed authorisation to send one prepared message (never sends without it)."""
    return _respond(get_flow().process_message_decision(body, datetime.now(UTC)))


@app.post("/v1/onboarding/workflow", tags=["workflow"])
def run_workflow(body: WorkflowBody) -> dict[str, Any]:
    """Run the UC-02 onboarding workflow end to end.

    Invokes the real service and the real agent loop. A repeated request for
    the same employee returns the existing case with ``created=false`` rather
    than creating a second one.
    """
    if body.llm_behaviour not in FAKE_BEHAVIOURS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_REQUEST",
                "message": "unknown llm_behaviour",
                "allowed": list(FAKE_BEHAVIOURS),
            },
        )

    request = WorkflowRequest(
        trigger=body.trigger,
        fixture=body.fixture,
        as_of=body.as_of,
        actor_id=body.actor_id,
        actor_role=body.actor_role,
        llm_behaviour=body.llm_behaviour,
        prepare_reminders=body.prepare_reminders,
        outlook_approval_id=body.outlook_approval_id,
    )

    agent = get_sub_agent()  # raises a clear 503 when the Supabase runtime is not configured or reachable
    try:
        return agent.run(request).as_dict()
    except OnboardingError as exc:
        status = next(
            (s for cls, s in STATUS_BY_ERROR.items() if isinstance(exc, cls)), 500
        )
        if status == 500:
            log.exception("unmapped domain error")
            raise HTTPException(
                status_code=500,
                detail={"code": "INTERNAL_ERROR", "message": "workflow failed"},
            ) from exc
        # Domain messages are written for operators and carry ids only.
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except Exception as exc:
        log.exception("unexpected workflow failure")
        raise HTTPException(
            status_code=500,
            detail={"code": "INTERNAL_ERROR", "message": "workflow failed"},
        ) from exc


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
