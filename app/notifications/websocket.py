"""WebSocketNotifier: fan each event out to the sockets watching that run; dead sockets are pruned and a
run.completed frame is followed by a normal close."""
from __future__ import annotations

import logging

from fastapi import WebSocket

from app.core.models import ExecutionEvent
from app.notifications.base import Notifier

log = logging.getLogger("app.notify.websocket")


class WebSocketNotifier(Notifier):
    def __init__(self) -> None:
        self._sockets: dict[str, set[WebSocket]] = {}

    def register(self, run_id: str, ws: WebSocket) -> None:
        self._sockets.setdefault(run_id, set()).add(ws)

    def unregister(self, run_id: str, ws: WebSocket) -> None:
        sockets = self._sockets.get(run_id)
        if sockets is None:
            return
        sockets.discard(ws)
        if not sockets:
            del self._sockets[run_id]

    def watchers(self, run_id: str) -> int:
        return len(self._sockets.get(run_id, ()))

    async def notify(self, event: ExecutionEvent) -> None:
        sockets = list(self._sockets.get(event.run_id, ()))
        if not sockets:
            return
        frame = event.model_dump_json()
        for ws in sockets:
            try:
                await ws.send_text(frame)
                if event.type == "run.completed":
                    await ws.close(code=1000)
            except Exception as exc:  # prune, never affect the run
                log.info("notify.failed sink=websocket run_id=%s reason=%s; socket pruned",
                         event.run_id, f"{type(exc).__name__}: {exc}")
                self.unregister(event.run_id, ws)
        log.debug("notify.sent sink=websocket type=%s run_id=%s watchers=%d", event.type, event.run_id, len(sockets))

    async def close(self) -> None:
        for run_id, sockets in list(self._sockets.items()):
            for ws in list(sockets):
                try:
                    await ws.close(code=1001)
                except Exception:  # already gone
                    pass
            self._sockets.pop(run_id, None)
