"""Shared HTTP helpers for the real adapters."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.brokers.base import AmbiguousOutcomeError, AuthError, BrokerUnavailableError, RateLimitError

IST = ZoneInfo("Asia/Kolkata")
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError,
              httpx.UnsupportedProtocol, httpx.LocalProtocolError)


async def send(http: httpx.AsyncClient, request: httpx.Request, *, is_order: bool) -> httpx.Response:
    """Send one request, translating transport failures and auth/throttle statuses into BrokerErrors.
    A failure after an order may have landed (ambiguous); the same failure on a read is just unavailable."""
    try:
        resp = await http.send(request)
    except NEVER_SENT as exc:
        raise BrokerUnavailableError(f"could not reach broker ({type(exc).__name__})") from exc
    except httpx.HTTPError as exc:  # may have reached the broker
        if is_order:
            raise AmbiguousOutcomeError(f"order sent but no answer ({type(exc).__name__})") from exc
        raise BrokerUnavailableError(f"broker read failed ({type(exc).__name__})") from exc

    status, body = resp.status_code, resp.text[:300]
    if status >= 500:
        if is_order:
            raise AmbiguousOutcomeError(f"order sent, broker answered HTTP {status}", raw=body)
        raise BrokerUnavailableError(f"broker answered HTTP {status}", raw=body)
    if status == 429:
        raise RateLimitError("rate limited (HTTP 429)", retry_after=_retry_after(resp), raw=body)
    if status == 403 and not isinstance(json_or_text(resp), dict) and "exceeding access rate" in body.lower():
        raise RateLimitError("rate limited (Angel One 403)", raw=body)
    if status in (401, 403):
        raise AuthError(f"broker refused credentials (HTTP {status})", raw=body)
    return resp


def _retry_after(resp: httpx.Response) -> float | None:
    """Seconds to wait, from `Retry-After` (seconds) or `X-Retry-After-Ms`; None when the broker is silent."""
    seconds, millis = resp.headers.get("Retry-After"), resp.headers.get("X-Retry-After-Ms")
    try:
        if seconds is not None:
            return float(seconds)
        if millis is not None:
            return float(millis) / 1000
    except ValueError:
        pass
    return None


def json_or_text(resp: httpx.Response) -> Any:
    """The parsed JSON body, or the raw text when the body is not JSON (Angel's throttle page, HTML errors)."""
    try:
        return resp.json()
    except ValueError:
        return resp.text


def dec(value: Any) -> Decimal | None:
    """Decimal from a float or string price; 0 or empty means not known yet (e.g. avg price of an open order)."""
    if value in (None, "", 0, "0", "0.0"):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def next_ist(hour: int, minute: int = 0) -> datetime:
    """The next HH:MM in Asia/Kolkata as a UTC datetime; brokers expire tokens daily at a fixed IST hour."""
    now = datetime.now(IST)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)
