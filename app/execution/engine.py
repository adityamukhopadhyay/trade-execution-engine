"""The execution engine: drives every order of a RUNNING report to a terminal state."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from functools import partial

from app.brokers.base import (
    AmbiguousOutcomeError,
    AuthError,
    BrokerAdapter,
    BrokerError,
    BrokerUnavailableError,
    OrderRejectedError,
    RateLimitError,
)
from app.core.context import order_id_var, run_id_var
from app.core.models import (
    BrokerSession,
    ErrorCode,
    EventType,
    ExecutionEvent,
    ExecutionReport,
    Holding,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Phase,
    utcnow,
)
from app.execution.poller import apply_update, mark_terminal, poll_until_terminal
from app.execution.reconciler import reconcile
from app.execution.report import derive_run_status, summarize
from app.execution.retry import backoff_delay
from app.execution.store import RunRepository
from app.notifications.base import Notifier

log = logging.getLogger(__name__)

UNRESOLVED_MESSAGE = "sent but unacknowledged; not found in order book by tag; NOT resent -- verify at broker"
CRASH_MESSAGE = {False: "engine error; see log", True: "engine error after placement; verify at broker"}


@dataclass(frozen=True)
class EngineConfig:
    """Retry, lookup and polling budgets."""

    place_max_attempts: int = 3
    retry_base_delay_s: float = 0.25
    retry_max_delay_s: float = 5.0
    poll_interval_s: float = 1.0
    poll_timeout_s: float = 30.0
    ambiguous_lookup_attempts: int = 5
    ambiguous_lookup_interval_s: float = 2.0
    market_hours_warn: bool = True


class ExecutionEngine:
    def __init__(self, runs: RunRepository, notifier: Notifier, config: EngineConfig) -> None:
        self.runs = runs
        self.notifier = notifier
        self.config = config

    async def run(self, report: ExecutionReport, session: BrokerSession,
                  adapter: BrokerAdapter) -> ExecutionReport:
        run_id_var.set(report.run_id)
        try:
            await self._emit("run.started", report)
            await self._run_phases(report, session, adapter)
            after = await self._holdings_after(session, adapter)
            report.reconciliation = reconcile(report.plan.holdings_before, after, report.orders,
                                              same_day_visible=adapter.meta.holdings_show_same_day_fills)
        except Exception:  # engine bug: still finalize the report
            log.exception("run.crashed")
            for order in report.orders:
                if not order.status.is_terminal:
                    placed = bool(order.broker_order_id or order.submitted_at)
                    status = OrderStatus.UNKNOWN if placed else OrderStatus.FAILED
                    mark_terminal(order, status, ErrorCode.INTERNAL, CRASH_MESSAGE[placed])
        report.status = derive_run_status(report.orders)
        report.finished_at = utcnow()
        await self._emit("run.completed", report)
        return report

    async def _run_phases(self, report: ExecutionReport, session: BrokerSession,
                          adapter: BrokerAdapter) -> None:
        sells = [order for order in report.orders if order.phase is Phase.SELL]
        buys = [order for order in report.orders if order.phase is Phase.BUY]
        if sells:
            await self._run_phase(Phase.SELL, sells, report, session, adapter)
            if not await self._sell_gate_passes(report, sells, buys):
                return
        if buys:
            await self._run_phase(Phase.BUY, buys, report, session, adapter)

    async def _run_phase(self, phase: Phase, orders: list[OrderResult], report: ExecutionReport,
                         session: BrokerSession, adapter: BrokerAdapter) -> None:
        await self._emit("run.phase", report, phase=phase)
        await asyncio.gather(*(self._place_one(order, report, session, adapter) for order in orders))
        await poll_until_terminal(adapter, session, orders, interval_s=self.config.poll_interval_s,
                                  timeout_s=self.config.poll_timeout_s,
                                  on_update=partial(self._order_changed, report))

    async def _sell_gate_passes(self, report: ExecutionReport, sells: list[OrderResult],
                                buys: list[OrderResult]) -> bool:
        """BUYs are funded by the SELLs, so an unfilled SELL either halts them (default) or is waived."""
        shortfall = [order for order in sells if order.status is not OrderStatus.FILLED]
        if not shortfall:
            return True
        detail = ", ".join(f"{order.symbol} {order.status.value}" for order in shortfall)
        log.warning("phase.gate", extra={"shortfall": detail, "policy": report.on_sell_shortfall})
        if report.on_sell_shortfall == "continue":
            report.plan.warnings.append(f"SELL shortfall ({detail}); BUY phase continued as requested")
            return True
        for order in buys:
            mark_terminal(order, OrderStatus.SKIPPED, ErrorCode.SELL_PHASE_HALTED,
                          f"BUY phase halted: SELL shortfall on {detail}")
            await self._order_changed(report, order)
        return False

    async def _place_one(self, order: OrderResult, report: ExecutionReport, session: BrokerSession,
                         adapter: BrokerAdapter) -> None:
        """Place with bounded retries. Every exception ends here as an OrderResult; gather() sees none."""
        order_id_var.set(order.tag)
        request = OrderRequest(**order.model_dump(include=set(OrderRequest.model_fields)))
        for attempt in range(1, self.config.place_max_attempts + 1):
            order.attempts = attempt
            try:
                async with adapter.limiter.orders():
                    order.broker_order_id = await adapter.place_order(session, request)
            except RateLimitError as exc:
                await self._wait_to_retry(order, attempt, ErrorCode.RATE_LIMIT, exc.message, exc.retry_after)
                continue
            except BrokerUnavailableError as exc:
                await self._wait_to_retry(order, attempt, ErrorCode.BROKER_UNAVAILABLE, exc.message, None)
                continue
            except OrderRejectedError as exc:
                mark_terminal(order, OrderStatus.REJECTED, ErrorCode.REJECTED, exc.reason)
            except AuthError as exc:
                mark_terminal(order, OrderStatus.FAILED, ErrorCode.AUTH, exc.message)
            except AmbiguousOutcomeError as exc:
                order.status, order.submitted_at = OrderStatus.AMBIGUOUS, utcnow()
                order.error_message = exc.message
                await self._order_changed(report, order)
                await self._resolve_ambiguous(order, report, session, adapter)
                return
            except Exception as exc:  # contain adapter bugs to this order
                log.exception("order.internal_error")
                mark_terminal(order, OrderStatus.FAILED, ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}")
            else:
                order.status, order.submitted_at = OrderStatus.OPEN, utcnow()
                order.error_code = order.error_message = None  # clear earlier retry errors
            await self._order_changed(report, order)
            return
        mark_terminal(order, OrderStatus.FAILED, order.error_code or ErrorCode.INTERNAL,
                      order.error_message or "")
        await self._order_changed(report, order)

    async def _wait_to_retry(self, order: OrderResult, attempt: int, code: ErrorCode, message: str,
                             retry_after: float | None) -> None:
        """Retried errors mean the broker holds nothing (refused or never sent): a resend cannot duplicate."""
        order.error_code, order.error_message = code, message
        if attempt < self.config.place_max_attempts:
            log.info("order.retry", extra={"tag": order.tag, "attempt": attempt, "error_code": code.value})
            await asyncio.sleep(backoff_delay(attempt, retry_after, base=self.config.retry_base_delay_s,
                                              cap=self.config.retry_max_delay_s))

    async def _resolve_ambiguous(self, order: OrderResult, report: ExecutionReport, session: BrokerSession,
                                 adapter: BrokerAdapter) -> None:
        """The order may already sit at the broker, so it is only ever looked up by tag, never resent."""
        for attempt in range(1, self.config.ambiguous_lookup_attempts + 1):
            try:
                async with adapter.limiter.reads():
                    found = await adapter.find_order_by_tag(session, order.tag)
            except BrokerError as exc:
                log.warning("order.lookup_error %s", exc.message, extra={"tag": order.tag})
                found = None
            if found is not None:
                order.error_message = None
                apply_update(order, found)
                log.info("order.located", extra={"tag": order.tag, "status": order.status.value})
                await self._order_changed(report, order)
                return
            if attempt < self.config.ambiguous_lookup_attempts:
                await asyncio.sleep(self.config.ambiguous_lookup_interval_s)
        mark_terminal(order, OrderStatus.UNKNOWN, ErrorCode.AMBIGUOUS_UNRESOLVED, UNRESOLVED_MESSAGE)
        await self._order_changed(report, order)

    async def _holdings_after(self, session: BrokerSession, adapter: BrokerAdapter) -> list[Holding] | None:
        try:
            async with adapter.limiter.reads():
                return await adapter.get_holdings(session)
        except Exception as exc:  # reconciliation is advisory
            log.warning("run.reconcile_unavailable", extra={"error": str(exc)})
            return None

    async def _order_changed(self, report: ExecutionReport, order: OrderResult) -> None:
        token = order_id_var.set(order.tag)
        try:
            log.info("order.updated", extra={"tag": order.tag, "status": order.status.value})
            await self._emit("order.updated", report, phase=order.phase, order=order)
        finally:
            order_id_var.reset(token)

    async def _emit(self, type_: EventType, report: ExecutionReport, *, phase: Phase | None = None,
                    order: OrderResult | None = None) -> None:
        report.summary = summarize(report.orders)
        self.runs.save(report)
        # sinks may serialize late, so hand them a snapshot
        event = ExecutionEvent(type=type_, run_id=report.run_id, phase=phase,
                               order=order.model_copy(deep=True) if order else None,
                               report=report.model_copy(deep=True))
        try:
            await self.notifier.notify(event)
        except Exception:  # broken sinks must not stop runs
            log.exception("notify.failed")
