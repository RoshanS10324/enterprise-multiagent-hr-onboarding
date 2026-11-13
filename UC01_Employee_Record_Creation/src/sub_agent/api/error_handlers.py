"""One module, one place, for every exception -> HTTP response mapping.

Nothing here ever echoes exception messages or request input back to the
caller: a message could carry a value (salary, email, discrepancy data)
that leaked into an error string by accident elsewhere. Only exception
*class names* are logged, never messages.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..errors import AccessDeniedError, CaseNotFoundError, PolicyViolationError

logger = logging.getLogger("sub_agent.api")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(CaseNotFoundError)
    async def _case_not_found(request: Request, exc: CaseNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": "case_not_found"})

    @app.exception_handler(AccessDeniedError)
    async def _access_denied(request: Request, exc: AccessDeniedError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"error": "access_denied"})

    @app.exception_handler(PolicyViolationError)
    async def _policy_violation(request: Request, exc: PolicyViolationError) -> JSONResponse:
        return JSONResponse(
            status_code=403, content={"error": "policy_violation", "code": exc.code.value}
        )

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError) -> JSONResponse:
        # Generic by design: the message is never returned (it could carry
        # a value). See docs/known-gaps.md for the follow-up (structured
        # conflict codes per ValueError kind).
        logger.warning("conflict: %s", type(exc).__name__)
        return JSONResponse(status_code=409, content={"error": "conflict"})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default handler echoes the offending input value back
        # in each error - which would leak a salary sent in a bad request.
        # Keep only the field location and error type.
        errors = [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": "validation_error", "errors": errors})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception: %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"error": "internal"})
