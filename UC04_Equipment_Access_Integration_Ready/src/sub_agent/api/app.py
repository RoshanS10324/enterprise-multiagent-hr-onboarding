"""Local development HTTP API for UC-04 Equipment & Approved Access Management. Swagger UI is served at /docs.

LOCAL DEVELOPMENT AND TESTING ONLY. This is not a production API:
  - there is no authentication; caller roles are taken from the request body, so anyone who can reach the API can claim any role;
  - all state (cases, mock tickets, audit) lives in memory and is lost on restart;
  - the parent case registry and the IT service desk are the same mocks the tests use. No real ticket, account or record exists;
  - bind it to 127.0.0.1 only (run_api.py does).

The API adds no business logic. Every decision is made by the existing Uc04Service, reached through DirectEntry or
OrchestratedEntry. This module only validates HTTP input, calls the entry point and maps exceptions to HTTP errors.
"""

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Body, FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from sub_agent import IMPLEMENTATION_MODE
from sub_agent.adapters.case_context_mock import MockCaseContext
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.advisor import AdvisoryReport, FakeLlmAdvisor
from sub_agent.contracts import USER_REF, SubmissionReport, Uc04Input, Uc04Result
from sub_agent.entrypoints.direct import DirectEntry
from sub_agent.entrypoints.orchestrated import OrchestratedEntry, OrchestratorRequest, OrchestratorResponse
from sub_agent.fixtures import load
from sub_agent.guardrails import PrivacyViolation
from sub_agent.policy import Policy, PolicyViolation
from sub_agent.ports import ParentCase
from sub_agent.service import CaseMismatchError, DuplicateCaseError, Uc04Service, Uc04ValidationError, UnknownCaseError
from sub_agent.states import ParentCaseStatus
from sub_agent.store import IdempotencyConflict
from sub_agent.api.examples import EVALUATE_EXAMPLES, ORCHESTRATED_EXAMPLES, STATUS_EXAMPLES, SUBMIT_EXAMPLES
from sub_agent.api.scenarios import TAGS, operation_tags

API_VERSION = "0.2.0"
TAG_BASIC, TAG_APPROVALS, TAG_RULES, TAG_LIFECYCLE, TAG_IT_DESK, TAG_ORCHESTRATION, TAG_SECURITY, TAG_STATE = (
    t["name"] for t in TAGS.values())
DEV_ONLY = "DEV ONLY - "
LOCAL_ONLY_NOTICE = ("Local development and testing API. Mock IT desk and mock parent-case registry only. "
                     "No authentication. Not production-ready.")
RUN_ID = r"^[A-Za-z0-9._-]{1,60}$"  # same patterns as sub_agent.contracts.CallerContext
IDEMPOTENCY_KEY = r"^[A-Za-z0-9._-]{1,80}$"
DEV_RESET_DESCRIPTION = ("DEV ONLY. When true, all in-memory mock state (cases, mock tickets, audit, parent statuses) is cleared "
                         "before this call, so an example starts clean. Leave false for the second step of a multi-step scenario.")


# --------------------------------------------------------------------------- HTTP schemas
class EvaluateRequest(BaseModel):
    """Direct invocation: the caller context plus the UC-04 input exactly as the service defines it."""

    model_config = ConfigDict(extra="forbid")

    actor_id: str = Field(pattern=USER_REF, description="Synthetic user reference of the caller, for example SYN-USER-100.")
    roles: list[str] = Field(default_factory=list, description="Caller roles. The policy file lists the allowed ones. "
                             "In this local API they are not authenticated.")
    run_id: str | None = Field(default=None, pattern=RUN_ID, description="Optional correlation id.")
    idempotency_key: str | None = Field(default=None, pattern=IDEMPOTENCY_KEY,
                                        description="Reuse the same key with the same payload to replay a stored result; "
                                                    "the same key with a different payload is refused (409).")
    dev_reset_first: StrictBool = Field(default=False, description=DEV_RESET_DESCRIPTION)
    input: Uc04Input


class EvaluateWithAdvisoryResponse(BaseModel):
    """Composite response containing authoritative Uc04Result and advisory Fake LLM report."""

    result: Uc04Result
    advisory: AdvisoryReport


class OrchestratedApiRequest(OrchestratorRequest):
    """The existing orchestrator envelope plus the DEV ONLY reset flag (the envelope itself is unchanged)."""

    dev_reset_first: StrictBool = Field(default=False, description=DEV_RESET_DESCRIPTION)


class DevToolsDisabled(Exception):
    pass


class ErrorResponse(BaseModel):
    error: str = Field(description="Stable error code.")
    detail: str
    fields: list[str] | None = Field(default=None, description="Names of the invalid fields. Values are never echoed.")
    existing_case_id: str | None = None


class HealthResponse(BaseModel):
    status: str
    implementation_mode: str
    policy_enabled: bool
    autonomy: str
    notice: str


class InfoResponse(BaseModel):
    name: str
    version: str
    implementation_mode: str
    notice: str
    docs: str
    openapi: str
    health: str


class ParentStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ParentCaseStatus


class ResetResponse(BaseModel):
    status: str
    parent_cases: list[ParentCase]


ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Caller role not allowed, the policy kill switch is off, or a DEV ONLY option "
                                                 "was used while dev tools are disabled."},
    404: {"model": ErrorResponse, "description": "Unknown onboarding case. UC-04 never creates one."},
    409: {"model": ErrorResponse, "description": "Case mismatch, duplicate case, or idempotency key reused with a different payload."},
    422: {"model": ErrorResponse, "description": "Invalid request or sensitive content refused. Field names only, never values."},
    500: {"model": ErrorResponse, "description": "Unexpected internal error. No details are returned."},
}


# --------------------------------------------------------------------------- environment (mocks only)
@dataclass
class Environment:
    service: Uc04Service
    cases: MockCaseContext
    desk: MockITServiceDesk
    direct: DirectEntry
    orchestrated: OrchestratedEntry
    advisor: FakeLlmAdvisor


def build_environment(policy: Policy | None = None) -> Environment:
    """A fresh in-memory environment: the synthetic parent cases, a mock IT desk and the real Uc04Service."""
    cases = MockCaseContext([ParentCase.model_validate(c) for c in load("parent_cases.json")])
    desk = MockITServiceDesk()
    service = Uc04Service(cases=cases, it_desk=desk, policy=policy)
    advisor = FakeLlmAdvisor()
    return Environment(service, cases, desk, DirectEntry(service), OrchestratedEntry(service), advisor)


def _env(request: Request) -> Environment:
    return request.app.state.env


def _reset(request: Request) -> None:
    request.app.state.env = build_environment(request.app.state.policy)


def _maybe_reset(request: Request, dev_reset_first: bool) -> None:
    """DEV ONLY isolation between Swagger examples."""
    if dev_reset_first:
        if not request.app.state.dev_tools:
            raise DevToolsDisabled()
        _reset(request)


# --------------------------------------------------------------------------- error mapping
def _error(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "detail": detail, **extra})


def _field_names(errors: list[Any]) -> list[str]:
    names = {".".join(str(p) for p in e.get("loc", ()) if p != "body") or "(request)" for e in errors}
    return sorted(names)


def _invalid(fields: list[str]) -> JSONResponse:
    return _error(422, "INVALID_REQUEST", "The request does not match the UC-04 contract. Values are not echoed.", fields=fields)


def _register_error_handlers(app: FastAPI) -> None:
    async def on_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _invalid(_field_names(exc.errors()))

    async def on_pydantic_validation(_: Request, exc: ValidationError) -> JSONResponse:
        return _invalid(_field_names(exc.errors()))

    async def on_service_validation(_: Request, exc: Uc04ValidationError) -> JSONResponse:
        return _invalid(sorted(exc.fields))

    async def on_policy(_: Request, exc: PolicyViolation) -> JSONResponse:
        return _error(403, "CALLER_NOT_PERMITTED", str(exc))

    async def on_unknown_case(_: Request, __: UnknownCaseError) -> JSONResponse:
        return _error(404, "UNKNOWN_CASE", "No such onboarding case in the parent registry. UC-04 never creates one.")

    async def on_mismatch(_: Request, __: CaseMismatchError) -> JSONResponse:
        return _error(409, "CASE_MISMATCH", "The employee or client does not match the parent case.")

    async def on_duplicate_case(_: Request, exc: DuplicateCaseError) -> JSONResponse:
        return _error(409, "DUPLICATE_CASE", "An onboarding case already exists for this employee. It is linked, not duplicated.",
                      existing_case_id=exc.existing_case_id)

    async def on_idempotency(_: Request, __: IdempotencyConflict) -> JSONResponse:
        return _error(409, "IDEMPOTENCY_CONFLICT", "The idempotency key was already used with a different payload.")

    async def on_privacy(_: Request, exc: PrivacyViolation) -> JSONResponse:
        return _error(422, "PRIVACY_GUARD", str(exc))  # the guard reports labels, never values

    async def on_unexpected(_: Request, __: Exception) -> JSONResponse:
        return _error(500, "INTERNAL_ERROR", "Unexpected error. Details are not returned.")

    async def on_dev_disabled(_: Request, __: DevToolsDisabled) -> JSONResponse:
        return _error(403, "DEV_TOOLS_DISABLED", "DEV ONLY options are disabled in this API instance.")

    for exc_type, handler in ((DevToolsDisabled, on_dev_disabled), (RequestValidationError, on_request_validation),
                              (ValidationError, on_pydantic_validation),
                              (Uc04ValidationError, on_service_validation), (PolicyViolation, on_policy),
                              (UnknownCaseError, on_unknown_case), (CaseMismatchError, on_mismatch),
                              (DuplicateCaseError, on_duplicate_case), (IdempotencyConflict, on_idempotency),
                              (PrivacyViolation, on_privacy), (Exception, on_unexpected)):
        app.add_exception_handler(exc_type, handler)


# --------------------------------------------------------------------------- routes
# One operation can carry several category tags: Swagger lists it under each, so the existing endpoints are not duplicated.
sub_agent = APIRouter(prefix="/api/v1/sub_agent", responses=ERRORS)
dev = APIRouter(prefix="/api/v1/dev", responses={404: ERRORS[404], 422: ERRORS[422]})


@sub_agent.post("/evaluate", response_model=Uc04Result, tags=operation_tags("evaluate"),
           summary="Evaluate a case (direct entry point)")
async def evaluate(request: Request, body: EvaluateRequest = Body(openapi_examples=EVALUATE_EXAMPLES)) -> Uc04Result:
    """Runs `DirectEntry.evaluate`. Decides status, blockers, risks, proposals and next actions. Never calls the IT desk,
    never creates an onboarding case, and never executes a proposal.

    Pick a numbered example from the drop-down: it states its purpose, fixture, steps, expected HTTP status and what to inspect."""
    _maybe_reset(request, body.dev_reset_first)
    return _env(request).direct.evaluate(body.input, actor_id=body.actor_id, roles=body.roles, run_id=body.run_id,
                                         idempotency_key=body.idempotency_key)




@sub_agent.post("/evaluate-with-advisory", response_model=EvaluateWithAdvisoryResponse, tags=[TAG_BASIC],
                summary="Evaluate a case and add the simulated (Fake LLM) advisory explanation")
async def evaluate_with_advisory(request: Request, body: EvaluateRequest = Body(openapi_examples=EVALUATE_EXAMPLES)
                                 ) -> EvaluateWithAdvisoryResponse:
    """Runs `DirectEntry.evaluate` exactly as `/evaluate` does, then explains the verified result with the local Fake LLM advisor.
    The `result` is the authoritative decision. The `advisory` is a deterministic, read-only explanation built from that result, the brain
    knowledge and the skills; it is labelled as simulated, changes nothing, and cannot approve, submit or notify anyone."""
    _maybe_reset(request, body.dev_reset_first)
    env = _env(request)
    result = env.direct.evaluate(body.input, actor_id=body.actor_id, roles=body.roles, run_id=body.run_id,
                                 idempotency_key=body.idempotency_key)
    return EvaluateWithAdvisoryResponse(result=result, advisory=env.advisor.generate_advisory(result, body.input))


@sub_agent.post("/submit-eligible", response_model=SubmissionReport, tags=operation_tags("submit"),
           summary="Evaluate, then submit eligible items to the MOCK IT desk")
async def submit_eligible(request: Request, body: EvaluateRequest = Body(openapi_examples=SUBMIT_EXAMPLES)) -> SubmissionReport:
    """Runs `DirectEntry.submit_eligible`. Only fully approved items are submitted, and only to the in-memory mock desk."""
    _maybe_reset(request, body.dev_reset_first)
    return _env(request).direct.submit_eligible(body.input, actor_id=body.actor_id, roles=body.roles, run_id=body.run_id,
                                                idempotency_key=body.idempotency_key)


@sub_agent.post("/orchestrated/evaluate", response_model=OrchestratorResponse, tags=operation_tags("orchestrated"),
           summary="Evaluate a case as the parent workflow would (orchestrated entry point)")
async def orchestrated_evaluate(request: Request, body: OrchestratedApiRequest = Body(openapi_examples=ORCHESTRATED_EXAMPLES)
                                ) -> OrchestratorResponse:
    """Runs `OrchestratedEntry.evaluate`. The envelope mirrors the orchestrator contract; `workflow.instance` must equal
    `input.case_id`. Same business decision as the direct path for equivalent input."""
    _maybe_reset(request, body.dev_reset_first)
    return _env(request).orchestrated.evaluate(body)


@dev.get("/mock-parent-cases", response_model=list[ParentCase], tags=[TAG_STATE],
         summary=DEV_ONLY + "List the mock parent onboarding cases")
async def list_parent_cases(request: Request) -> list[ParentCase]:
    cases = _env(request).cases
    return [cases.get_case(case_id) for case_id in cases.case_ids]


@dev.put("/mock-parent-cases/{case_id}/status", response_model=ParentCase, tags=[TAG_LIFECYCLE, TAG_STATE],
         summary=DEV_ONLY + "Set a mock parent case status (ON_HOLD, WITHDRAWN, ...)")
async def set_parent_status(request: Request, body: ParentStatusUpdate = Body(openapi_examples=STATUS_EXAMPLES),
                            case_id: str = Path(examples=["SYN-CASE-001", "SYN-CASE-002", "SYN-CASE-003", "SYN-CASE-004"])
                            ) -> ParentCase:
    """DEV ONLY. Changes only the in-memory mock registry, so the on-hold and withdrawn scenarios can be tested."""
    cases = _env(request).cases
    if cases.get_case(case_id) is None:
        raise UnknownCaseError(case_id)
    cases.set_status(case_id, body.status)
    return cases.get_case(case_id)


@dev.post("/reset", response_model=ResetResponse, tags=[TAG_STATE],
          summary=DEV_ONLY + "Reset all in-memory state (cases, mock tickets, audit, parent statuses)")
async def reset(request: Request) -> ResetResponse:
    """DEV ONLY. Examples normally reset themselves (`dev_reset_first`); use this for the manual steps of a multi-step scenario."""
    _reset(request)
    cases = _env(request).cases
    return ResetResponse(status="reset", parent_cases=[cases.get_case(c) for c in cases.case_ids])


# --------------------------------------------------------------------------- app
def _tag_metadata() -> list[dict[str, str]]:
    return [{"name": t["name"], "description": t["description"] + "\n\n**Limitations:** " + " ".join(t["limits"])}
            for t in TAGS.values()]


def create_app(policy: Policy | None = None, dev_tools: bool = True) -> FastAPI:
    """`dev_tools=False` removes the DEV ONLY endpoints and refuses `dev_reset_first`."""
    app = FastAPI(
        title="Enterprise HR Agent UC-04 Equipment & Approved Access (local development API)", version=API_VERSION,
        description=f"**{LOCAL_ONLY_NOTICE}**\n\nExposes the existing deterministic `Uc04Service`. No LLM, no network, no real "
                    "tickets or accounts. All identifiers are synthetic (`SYN-...`).\n\n"
                    "Swagger groups HTTP endpoints, not business capabilities, so each endpoint is listed under every category "
                    "it can demonstrate. Choose a numbered example from the request-body drop-down; each states its purpose, "
                    "fixture, steps, expected HTTP status and what to inspect. DEV ONLY options only change in-memory mock state.",
        openapi_tags=_tag_metadata(), docs_url="/docs", openapi_url="/openapi.json")
    app.state.policy = policy
    app.state.dev_tools = dev_tools
    app.state.env = build_environment(policy)
    _register_error_handlers(app)

    @app.get("/", response_model=InfoResponse, tags=[TAG_BASIC], summary="API information")
    async def info() -> InfoResponse:
        return InfoResponse(name="Enterprise HR Agent UC-04 Equipment & Approved Access (local development API)", version=API_VERSION,
                            implementation_mode=IMPLEMENTATION_MODE, notice=LOCAL_ONLY_NOTICE, docs="/docs",
                            openapi="/openapi.json", health="/health")

    @app.get("/health", response_model=HealthResponse, tags=[TAG_SECURITY], summary="API health and policy status")
    async def health(request: Request) -> HealthResponse:
        policy_in_use = _env(request).service.policy
        return HealthResponse(status="ok", implementation_mode=IMPLEMENTATION_MODE, policy_enabled=policy_in_use.enabled,
                              autonomy=policy_in_use.autonomy.value, notice=LOCAL_ONLY_NOTICE)

    app.include_router(sub_agent)
    if dev_tools:
        app.include_router(dev)
    return app


app = create_app()
