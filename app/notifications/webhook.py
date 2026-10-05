"""WebhookNotifier: POST selected events as JSON to one URL. One retry, then give up. Never raises."""
from __future__ import annotations

import logging

import httpx

from app.core.models import ExecutionEvent
from app.notifications.base import Notifier

log = logging.getLogger("app.notify.webhook")
DEFAULT_EVENTS: frozenset[str] = frozenset({"run.completed"})


class WebhookNotifier(Notifier):
    def __init__(self, http: httpx.AsyncClient, url: str, *, timeout_s: float = 5.0,
                 events: set[str] | frozenset[str] | None = None) -> None:
        self._http = http
        self._url = url
        self._timeout = timeout_s
        self._events = frozenset(events) if events is not None else DEFAULT_EVENTS

    async def notify(self, event: ExecutionEvent) -> None:
        if event.type not in self._events:
            return
        body = event.model_dump(mode="json")
        last_failure = "unknown"
        for attempt in (1, 2):
            try:
                response = await self._http.post(self._url, json=body, timeout=self._timeout)
            except httpx.HTTPError as exc:
                last_failure = f"{type(exc).__name__}: {exc}"
            else:
                if response.is_success:
                    log.info("notify.sent sink=webhook type=%s run_id=%s status=%s attempt=%d",
                             event.type, event.run_id, response.status_code, attempt)
                    return
                last_failure = f"HTTP {response.status_code}"
            log.warning("notify.retry sink=webhook type=%s run_id=%s attempt=%d reason=%s",
                        event.type, event.run_id, attempt, last_failure)
        log.error("notify.failed sink=webhook type=%s run_id=%s reason=%s", event.type, event.run_id, last_failure)
