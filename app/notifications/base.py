"""Notifier contract. The engine emits ExecutionEvent frames; sinks deliver them and never raise."""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.models import ExecutionEvent


class Notifier(ABC):
    @abstractmethod
    async def notify(self, event: ExecutionEvent) -> None:
        """Deliver one event. Must swallow and log its own failures: a dead sink never changes a run."""

    async def close(self) -> None:
        """Release resources at shutdown (sockets, clients). Default: nothing."""
