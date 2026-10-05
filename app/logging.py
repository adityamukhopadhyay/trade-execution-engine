"""Structured JSON logging with run/order correlation ids and credential redaction."""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import SecretStr

from app.core.context import order_id_var, run_id_var

REDACT_KEYS: frozenset[str] = frozenset({
    "authorization", "access_token", "api_secret", "api_key", "password", "mpin", "totp", "request_token",
    "auth_code", "code", "client_secret", "x-privatekey", "token", "jwttoken", "credentials",
    "app_secret", "refresh_token",
})
MASK = "***"
HANDLER_NAME = "trade-execution-engine"
TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s run=%(run_id)s order=%(order_id)s %(message)s"

# anything not here came from extra=
_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("x", 0, "x", 0, "", (), None))) | {"message", "asctime"}
_KEY_VALUE_RE = re.compile(r"(?i)(\b(?:" + "|".join(sorted(REDACT_KEYS)) + r")\b[\"']?\s*[:=]\s*)(\S+)")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")


def scrub(value: Any) -> Any:
    """Return `value` with secrets replaced by MASK, recursing into dicts and lists."""
    if isinstance(value, SecretStr):
        return MASK
    if isinstance(value, dict):
        return {k: MASK if str(k).lower() in REDACT_KEYS else scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return mask_text(value)
    return value


def mask_text(text: str) -> str:
    """Mask `access_token=abc`, `password: x`, `"token": "x"` and `Bearer xyz` patterns inside free text."""
    text = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}{MASK}", text)
    return _BEARER_RE.sub(f"Bearer {MASK}", text)


class ContextFilter(logging.Filter):
    """Copy the engine's correlation ids onto the record (unless the caller already set them)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "run_id", None) is None:
            record.run_id = run_id_var.get()
        if getattr(record, "order_id", None) is None:
            record.order_id = order_id_var.get()
        return True


class RedactFilter(logging.Filter):
    """Mask credentials in the message, its args and every `extra` field. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_text(record.getMessage())
        record.args = ()
        for key, value in list(vars(record).items()):
            if key in _STANDARD_ATTRS:
                continue
            setattr(record, key, MASK if key.lower() in REDACT_KEYS else scrub(value))
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, run_id, order_id, then every `extra` field."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        payload: dict[str, Any] = {
            "ts": ts.replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "run_id": getattr(record, "run_id", None) or run_id_var.get(),
            "order_id": getattr(record, "order_id", None) or order_id_var.get(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: Literal["json", "text"] = "json") -> None:
    """Install the stdout handler (replacing one installed earlier) and re-point uvicorn's loggers."""
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(TEXT_FORMAT))
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True


_http_log = logging.getLogger("app.broker.http")


async def _stamp_start(request: httpx.Request) -> None:
    request.extensions["started_at"] = time.perf_counter()


async def _log_response(response: httpx.Response) -> None:
    started = response.request.extensions.get("started_at")
    elapsed_ms = int((time.perf_counter() - started) * 1000) if started else -1
    _http_log.info("broker.http method=%s url=%s status=%s elapsed_ms=%d",
                   response.request.method, response.request.url.path, response.status_code, elapsed_ms)


def http_log_hooks() -> dict[str, list[Any]]:
    """Event hooks for the shared httpx.AsyncClient: one line per call, no headers, no bodies."""
    return {"request": [_stamp_start], "response": [_log_response]}
