"""Exception to HTTP mapping; every error leaves as {"error": {"code", "message", "details"}}."""
from __future__ import annotations

import logging
import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.brokers.base import (
    AmbiguousOutcomeError,
    AuthError,
    BrokerError,
    BrokerUnavailableError,
    OrderRejectedError,
    RateLimitError,
)
from app.core.errors import ApiError

log = logging.getLogger("app.api")

# first match wins
BROKER_ERROR_MAP: tuple[tuple[type[BrokerError], int, str], ...] = (
    (AuthError, 401, "broker_auth"),
    (RateLimitError, 429, "broker_rate_limited"),
    (BrokerUnavailableError, 503, "broker_unavailable"),
    (AmbiguousOutcomeError, 503, "broker_unavailable"),
    (OrderRejectedError, 422, "broker_rejected"),
)


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    log.info("api.error error_code=%s status=%d path=%s", exc.code, exc.status_code, request.url.path)
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


async def handle_broker_error(request: Request, exc: BrokerError) -> JSONResponse:
    status, code = 502, "broker_error"
    for exc_type, mapped_status, mapped_code in BROKER_ERROR_MAP:
        if isinstance(exc, exc_type):
            status, code = mapped_status, mapped_code
            break
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimitError) and exc.retry_after is not None:
        headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after)))
    log.info("api.error error_code=%s status=%d path=%s broker_message=%s",
             code, status, request.url.path, exc.message)
    return JSONResponse(status_code=status, content=error_body(code, exc.message), headers=headers)


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [{"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return JSONResponse(status_code=422, content=error_body("request_invalid", "request body is invalid", details))


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Anything else: the envelope with no exception text; the traceback goes to the server log only."""
    log.error("api.unhandled error_type=%s path=%s", type(exc).__name__, request.url.path)
    return JSONResponse(status_code=500, content=error_body("internal", "internal error; see the server log"))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, handle_api_error)  # type: ignore[arg-type]
    app.add_exception_handler(BrokerError, handle_broker_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, handle_unexpected_error)
