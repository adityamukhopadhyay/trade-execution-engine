"""Front door for an execution request: everything before the engine starts placing orders."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

from app.core.errors import IdempotencyConflictError, PortfolioInvalidError, RunNotFoundError
from app.core.instruments import InstrumentTable
from app.core.models import ExecuteRequest, ExecutionPlan, ExecutionReport, OrderResult, RunStatus, utcnow
from app.core.session_store import SessionStore
from app.execution.engine import EngineConfig, ExecutionEngine
from app.execution.planner import build_plan
from app.execution.report import summarize
from app.execution.store import RunRepository
from app.execution.validator import validate

if TYPE_CHECKING:  # avoids importing every adapter
    from app.brokers.registry import BrokerRegistry


class ExecutionService:
    def __init__(self, sessions: SessionStore, registry: BrokerRegistry, runs: RunRepository,
                 engine: ExecutionEngine, instruments: InstrumentTable,
                 config: EngineConfig | None = None) -> None:
        self.sessions = sessions
        self.registry = registry
        self.runs = runs
        self.engine = engine
        self.instruments = instruments
        self.config = config or engine.config
        self._tasks: set[asyncio.Task[ExecutionReport]] = set()  # strong refs; untracked tasks get GC'd
        self._key_locks: dict[str, asyncio.Lock] = {}

    async def submit(self, req: ExecuteRequest, *, wait: bool = False) -> tuple[ExecutionReport, bool]:
        """Returns (report, replayed). Raises ApiError subclasses or BrokerError before anything is placed."""
        async with self._one_at_a_time(req.idempotency_key):
            replay = self._replay(req)
            if replay is not None:
                return replay, True
            session = self.sessions.get(req.session_id)
            adapter = self.registry.get(session.broker)
            async with adapter.limiter.reads():
                holdings = await adapter.get_holdings(session)
            issues = validate(req.portfolio, holdings, self.instruments,
                              allow_existing_holdings=req.allow_existing_holdings)
            if issues:
                raise PortfolioInvalidError("portfolio rejected; nothing was placed", details=issues)
            plan = build_plan(uuid4().hex, session, req.portfolio, holdings, self.instruments,
                              market_hours_warn=self.config.market_hours_warn)
            report = self._new_report(req, plan)
            self.runs.save(report)
            if req.idempotency_key:
                self.runs.bind_key(req.idempotency_key, req.payload_hash(), report.run_id)
        if req.dry_run:
            return report, False
        task = asyncio.create_task(self.engine.run(report, session, adapter), name=f"run-{report.run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if wait:
            await task
        return report, False

    def get(self, run_id: str) -> ExecutionReport:
        report = self.runs.get(run_id)
        if report is None:
            raise RunNotFoundError(f"run {run_id} not found")
        return report

    def list(self, limit: int, session_id: str | None = None) -> list[ExecutionReport]:
        return self.runs.list(limit, session_id)

    async def aclose(self, *, grace_s: float = 5.0) -> None:
        """Let in-flight runs finish for `grace_s`, then cancel what is left so the process can stop."""
        running = [task for task in self._tasks if not task.done()]
        if not running:
            return
        _, pending = await asyncio.wait(running, timeout=grace_s)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    @asynccontextmanager
    async def _one_at_a_time(self, key: str | None) -> AsyncIterator[None]:
        """Pre-flight runs under a per-key lock, so a concurrent retry finds the binding and replays."""
        if key is None:
            yield
            return
        lock = self._key_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                yield
        finally:
            if not lock.locked():
                self._key_locks.pop(key, None)

    def _replay(self, req: ExecuteRequest) -> ExecutionReport | None:
        """Same key + same payload -> the stored report; a different payload or an evicted run -> 409."""
        if not req.idempotency_key:
            return None
        bound = self.runs.find_by_key(req.idempotency_key)
        if bound is None:
            return None
        run_id, payload_hash = bound
        if payload_hash != req.payload_hash():
            raise IdempotencyConflictError(f"idempotency_key {req.idempotency_key!r} was already used "
                                           "with a different payload")
        report = self.runs.get(run_id)
        if report is None:
            raise IdempotencyConflictError(f"idempotency_key {req.idempotency_key!r} belongs to run {run_id}, "
                                           "which is no longer stored; use a new key")
        return report

    @staticmethod
    def _new_report(req: ExecuteRequest, plan: ExecutionPlan) -> ExecutionReport:
        orders = [OrderResult.from_planned(order) for order in plan.orders]
        return ExecutionReport(
            run_id=plan.run_id, idempotency_key=req.idempotency_key, broker=plan.broker, mode=plan.mode,
            status=RunStatus.PLANNED if req.dry_run else RunStatus.RUNNING, dry_run=req.dry_run,
            on_sell_shortfall=req.on_sell_shortfall, plan=plan, orders=orders, summary=summarize(orders),
            started_at=utcnow(),
        )
