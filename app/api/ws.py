"""WS /ws/executions/{run_id}: a run.snapshot frame on connect, every live ExecutionEvent, then a normal
close (1000) once run.completed has been sent."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.api.deps import get_service, get_ws_notifier
from app.core.errors import RunNotFoundError
from app.core.models import ExecutionEvent, RunStatus
from app.execution.service import ExecutionService
from app.notifications.websocket import WebSocketNotifier

log = logging.getLogger("app.api.ws")
router = APIRouter(tags=["notifications"])

CLOSE_RUN_NOT_FOUND = 4404


@router.websocket("/ws/executions/{run_id}")
async def execution_stream(
    websocket: WebSocket, run_id: str,
    service: Annotated[ExecutionService, Depends(get_service)],
    ws_notifier: Annotated[WebSocketNotifier, Depends(get_ws_notifier)],
) -> None:
    await websocket.accept()
    try:
        report = service.get(run_id)
    except RunNotFoundError:
        await websocket.close(code=CLOSE_RUN_NOT_FOUND, reason="run not found")
        return

    snapshot = ExecutionEvent(type="run.snapshot", run_id=run_id, report=report)
    ws_notifier.register(run_id, websocket)  # before the send, so a run finishing meanwhile still reaches us
    try:
        live = report.status is RunStatus.RUNNING
        await websocket.send_text(snapshot.model_dump_json())
        if not live:
            await websocket.close(code=1000)
            return
        while websocket.application_state is WebSocketState.CONNECTED:  # the notifier closes on run.completed
            text = await websocket.receive_text()
            if text.strip() == "ping" and websocket.application_state is WebSocketState.CONNECTED:
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        log.debug("ws.closed run_id=%s", run_id)
    finally:
        ws_notifier.unregister(run_id, websocket)
