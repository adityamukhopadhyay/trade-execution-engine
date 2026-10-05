"""Where ExecutionReports live, plus the idempotency-key index."""
from __future__ import annotations

from abc import ABC, abstractmethod
from itertools import islice

from app.core.models import ExecutionReport


class RunRepository(ABC):
    @abstractmethod
    def save(self, report: ExecutionReport) -> None:
        """Insert or replace by run_id."""

    @abstractmethod
    def get(self, run_id: str) -> ExecutionReport | None: ...

    @abstractmethod
    def list(self, limit: int, session_id: str | None = None) -> list[ExecutionReport]:
        """Newest first, optionally only one session's runs."""

    @abstractmethod
    def find_by_key(self, key: str) -> tuple[str, str] | None:
        """(run_id, payload_hash) bound to an idempotency key, or None."""

    @abstractmethod
    def bind_key(self, key: str, payload_hash: str, run_id: str) -> None: ...


class InMemoryRunRepository(RunRepository):
    """Insertion-ordered dicts that drop their oldest entry: runs are capped at `limit`, idempotency
    bindings at `key_limit` (default 10x) so a binding outlives the run it points at."""

    def __init__(self, limit: int = 200, key_limit: int | None = None) -> None:
        self._limit = limit
        self._key_limit = key_limit or limit * 10
        self._runs: dict[str, ExecutionReport] = {}
        self._keys: dict[str, tuple[str, str]] = {}

    def save(self, report: ExecutionReport) -> None:
        self._runs[report.run_id] = report  # re-save keeps position
        while len(self._runs) > self._limit:
            del self._runs[next(iter(self._runs))]

    def get(self, run_id: str) -> ExecutionReport | None:
        return self._runs.get(run_id)

    def list(self, limit: int, session_id: str | None = None) -> list[ExecutionReport]:
        newest_first = reversed(self._runs.values())
        wanted = (r for r in newest_first if session_id is None or r.plan.session_id == session_id)
        return list(islice(wanted, limit))

    def find_by_key(self, key: str) -> tuple[str, str] | None:
        return self._keys.get(key)

    def bind_key(self, key: str, payload_hash: str, run_id: str) -> None:
        self._keys[key] = (run_id, payload_hash)
        while len(self._keys) > self._key_limit:
            del self._keys[next(iter(self._keys))]
