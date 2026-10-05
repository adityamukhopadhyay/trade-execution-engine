"""POST + GET /mock/webhook: the demo receiver for our own WebhookNotifier."""
from __future__ import annotations

import logging
from collections import deque
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Response

from app.api.deps import get_webhook_inbox

log = logging.getLogger("app.api.webhook")
router = APIRouter(prefix="/mock/webhook", tags=["mock"])

Inbox = Annotated[deque[Any], Depends(get_webhook_inbox)]


def describe(body: Any) -> dict[str, Any]:
    """Pull run_id / status / counts out of an ExecutionEvent body; tolerate any other JSON."""
    if not isinstance(body, dict):
        return {"run_id": None, "status": None, "filled": None, "failed": None}
    report = body.get("report") if isinstance(body.get("report"), dict) else {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    return {"run_id": body.get("run_id"), "status": report.get("status"),
            "filled": summary.get("filled"), "failed": summary.get("failed")}


@router.post("", status_code=204)
async def receive(body: Annotated[Any, Body()], inbox: Inbox) -> Response:
    inbox.appendleft(body)
    facts = describe(body)
    log.info("webhook.received run_id=%s status=%s filled=%s failed=%s",
             facts["run_id"], facts["status"], facts["filled"], facts["failed"])
    return Response(status_code=204)


@router.get("")
async def deliveries(inbox: Inbox) -> dict[str, list[Any]]:
    """The most recent bodies, newest first."""
    return {"deliveries": list(inbox)}
