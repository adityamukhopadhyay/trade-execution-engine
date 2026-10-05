"""API-facing exceptions, mapped to HTTP JSON in app/api/errors.py."""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class UnknownBrokerError(ApiError):
    status_code, code = 404, "unknown_broker"


class RunNotFoundError(ApiError):
    status_code, code = 404, "run_not_found"


class SessionNotFoundError(ApiError):
    """Missing *or expired* session id."""
    status_code, code = 401, "session_not_found"


class CredentialsMissingError(ApiError):
    """details = list of missing credential field names."""
    status_code, code = 422, "credentials_missing"


class SymbolNotFoundError(ApiError):
    """Symbol/exchange not in the instrument table. Raised before any order is placed."""
    status_code, code = 422, "symbol_unknown"


class PortfolioInvalidError(ApiError):
    """details = every issue as {"symbol": str | None, "message": str}, not just the first."""
    status_code, code = 422, "portfolio_invalid"


class IdempotencyConflictError(ApiError):
    """Same idempotency_key with a different payload, or its run is no longer stored."""
    status_code, code = 409, "idempotency_conflict"
