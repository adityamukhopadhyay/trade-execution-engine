"""FanoutNotifier: deliver one event to every sink at once. A failing sink is logged and isolated."""
from __future__ import annotations

import asyncio
import logging

from app.core.models import ExecutionEvent
from app.notifications.base import Notifier

log = logging.getLogger("app.notify.fanout")


class FanoutNotifier(Notifier):
    def __init__(self, notifiers: list[Notifier]) -> None:
        self._notifiers = list(notifiers)

    async def notify(self, event: ExecutionEvent) -> None:
        results = await asyncio.gather(*(n.notify(event) for n in self._notifiers), return_exceptions=True)
        for notifier, result in zip(self._notifiers, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("notify.failed sink=%s type=%s run_id=%s reason=%s",
                            type(notifier).__name__, event.type, event.run_id,
                            f"{type(result).__name__}: {result}")

    async def close(self) -> None:
        results = await asyncio.gather(*(n.close() for n in self._notifiers), return_exceptions=True)
        for notifier, result in zip(self._notifiers, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("notify.close_failed sink=%s reason=%s", type(notifier).__name__, result)
