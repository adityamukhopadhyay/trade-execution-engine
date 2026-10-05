"""ConsoleNotifier: one log line per execution event. With the JSON formatter that is one JSON line."""
from __future__ import annotations

import logging

from app.core.models import ExecutionEvent
from app.notifications.base import Notifier


class ConsoleNotifier(Notifier):
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or logging.getLogger("app.notify.console")

    async def notify(self, event: ExecutionEvent) -> None:
        summary = event.report.summary
        order = event.order
        self._log.info(
            "notify.sent sink=console type=%s run_id=%s status=%s phase=%s order=%s total=%d filled=%d "
            "rejected=%d failed=%d timed_out=%d unknown=%d skipped=%d",
            event.type, event.run_id, event.report.status.value, event.phase.value if event.phase else "-",
            f"{order.tag}:{order.status.value}" if order else "-", summary.total, summary.filled,
            summary.rejected, summary.failed, summary.timed_out, summary.unknown, summary.skipped,
            extra={"run_id": event.run_id, "event_type": event.type, "run_status": event.report.status.value},
        )
