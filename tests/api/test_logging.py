"""Logging: JSON shape, credential redaction, correlation ids, and no secret ever reaching the log."""
from __future__ import annotations

import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from app.core.context import order_id_var, run_id_var
from app.logging import (
    HANDLER_NAME,
    MASK,
    REDACT_KEYS,
    ContextFilter,
    JsonFormatter,
    RedactFilter,
    configure_logging,
    mask_text,
)

SECRET = "sk-9f8e7d6c5b4a-SECRET"


def make_record(msg: str, *args: object, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, msg, args, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_shape() -> None:
    record = make_record("hello %s", "world", broker="fake")
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "hello world"
    assert payload["level"] == "INFO" and payload["logger"] == "app.test"
    assert payload["ts"].endswith("Z") and payload["broker"] == "fake"
    assert "run_id" in payload and "order_id" in payload


def test_redact_filter_masks_extras_secretstr_and_message() -> None:
    record = make_record(
        "login access_token=%s header Bearer %s password: %s", SECRET, SECRET, SECRET,
        access_token=SECRET, credentials={"api_key": SECRET, "user": "u1"},
        nested={"deep": {"totp": "123456"}, "tok": SecretStr(SECRET)}, plain="kept",
    )
    assert RedactFilter().filter(record) is True
    assert SECRET not in record.getMessage() and "123456" not in json.dumps(vars(record), default=str)
    assert record.getMessage() == f"login access_token={MASK} header Bearer {MASK} password: {MASK}"
    assert record.access_token == MASK  # type: ignore[attr-defined]
    assert record.credentials == MASK  # type: ignore[attr-defined]
    assert record.nested == {"deep": {"totp": MASK}, "tok": MASK}  # type: ignore[attr-defined]
    assert record.plain == "kept"  # type: ignore[attr-defined]


@pytest.mark.parametrize("key", sorted(REDACT_KEYS))
def test_every_redact_key_is_masked_in_text(key: str) -> None:
    assert mask_text(f"x {key}={SECRET} y") == f"x {key}={MASK} y"


def test_json_quoted_keys_are_masked_in_text() -> None:
    masked = mask_text(f'{{"access_token": "{SECRET}", "user": "u1"}}')
    assert SECRET not in masked and '"access_token": ***' in masked and '"user": "u1"' in masked


def test_context_filter_stamps_correlation_ids() -> None:
    run_token, order_token = run_id_var.set("run-xyz"), order_id_var.set("kp12345678001")
    try:
        record = make_record("placing")
        ContextFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))
    finally:
        run_id_var.reset(run_token)
        order_id_var.reset(order_token)
    assert record.run_id == "run-xyz" and payload["order_id"] == "kp12345678001"  # type: ignore[attr-defined]


def test_configure_logging_installs_exactly_one_handler() -> None:
    configure_logging("DEBUG", "json")
    configure_logging("INFO", "text")
    root = logging.getLogger()
    ours = [h for h in root.handlers if h.get_name() == HANDLER_NAME]
    assert len(ours) == 1 and root.level == logging.INFO
    assert not logging.getLogger("uvicorn.access").handlers


async def test_session_create_never_logs_credentials(client: httpx.AsyncClient,
                                                     caplog: pytest.LogCaptureFixture) -> None:
    caplog.handler.addFilter(ContextFilter())
    caplog.handler.addFilter(RedactFilter())
    with caplog.at_level(logging.DEBUG):
        response = await client.post("/brokers/fake/sessions", json={"credentials": {"api_key": SECRET}})
        logging.getLogger("app.test").info("simulated careless line api_key=%s", SECRET)
    assert response.status_code == 201
    assert SECRET not in caplog.text
    assert f"api_key={MASK}" in caplog.text
