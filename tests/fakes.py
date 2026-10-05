"""Test doubles for the engine: a scriptable broker, a recording notifier and payload builders."""
from __future__ import annotations

from collections import deque
from typing import Any

import httpx

from app.brokers.base import BrokerAdapter, BrokerMeta, RateLimits
from app.core.context import order_id_var, run_id_var
from app.core.errors import UnknownBrokerError
from app.core.instruments import InstrumentTable
from app.core.models import (
    BrokerInfo,
    BrokerSession,
    ExecutionEvent,
    ExecutionPlan,
    ExecutionReport,
    FirstTimePayload,
    Holding,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderUpdate,
    RebalanceInstruction,
    RebalancePayload,
    RunStatus,
    utcnow,
)
from app.execution.report import summarize
from app.notifications.base import Notifier


def _unwrap(scripted: Any) -> Any:
    if isinstance(scripted, Exception):
        raise scripted
    return scripted


class ScriptedAdapter(BrokerAdapter):
    """Answers from per-method queues of values or exceptions (the last poll observation sticks) and
    records every call; unscripted placements get a fresh id and fill on their first poll."""

    meta = BrokerMeta(name="scripted", display_name="Scripted (tests)", required_credentials=(),
                      limits=RateLimits(1000, 100_000, 1000), live_tested=True,
                      holdings_show_same_day_fills=True)

    def __init__(self, holdings: list[Holding] | None = None) -> None:
        super().__init__(httpx.AsyncClient(), InstrumentTable([]), {"max_inflight": 64})
        self.holdings: list[Holding] = list(holdings or [])
        self.holdings_queue: deque[list[Holding] | Exception] = deque()
        self.place_queue: dict[str, deque[str | Exception]] = {}
        self.poll_queue: dict[str, deque[OrderUpdate | Exception]] = {}
        self.book: dict[str, OrderUpdate] = {}  # what list_orders() can see
        self.calls: list[tuple[str, str | None]] = []  # (method, tag or broker id)
        self.contexts: list[tuple[str | None, str | None]] = []  # (run_id_var, order_id_var) per place
        self._ids_issued = 0

    def script_place(self, tag: str, *outcomes: str | Exception) -> None:
        self.place_queue.setdefault(tag, deque()).extend(outcomes)

    def script_poll(self, broker_order_id: str, *observations: OrderUpdate | Exception) -> None:
        self.poll_queue.setdefault(broker_order_id, deque()).extend(observations)

    def seed_book(self, broker_order_id: str, tag: str, status: OrderStatus = OrderStatus.OPEN,
                  filled_qty: int = 0) -> None:
        self.book[broker_order_id] = OrderUpdate(broker_order_id=broker_order_id, status=status,
                                                 filled_qty=filled_qty, tag=tag)

    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)

    def placed_tags(self) -> list[str]:
        return [arg or "" for name, arg in self.calls if name == "place_order"]

    async def complete_login(self, credentials: dict[str, str]) -> BrokerSession:
        raise NotImplementedError("tests build BrokerSession objects directly")

    async def get_holdings(self, session: BrokerSession) -> list[Holding]:
        self.calls.append(("get_holdings", None))
        if self.holdings_queue:
            return _unwrap(self.holdings_queue.popleft())
        return list(self.holdings)

    async def place_order(self, session: BrokerSession, order: OrderRequest) -> str:
        self.calls.append(("place_order", order.tag))
        self.contexts.append((run_id_var.get(), order_id_var.get()))
        queue = self.place_queue.get(order.tag)
        broker_order_id = _unwrap(queue.popleft()) if queue else self._new_id()
        self.seed_book(broker_order_id, order.tag)
        return broker_order_id

    async def get_order(self, session: BrokerSession, broker_order_id: str) -> OrderUpdate:
        self.calls.append(("get_order", broker_order_id))
        return self._observe(broker_order_id)

    async def list_orders(self, session: BrokerSession) -> list[OrderUpdate]:
        self.calls.append(("list_orders", None))
        return [self._observe(broker_order_id) for broker_order_id in list(self.book)]

    async def find_order_by_tag(self, session: BrokerSession, tag: str) -> OrderUpdate | None:
        self.calls.append(("find_order_by_tag", tag))
        return await super().find_order_by_tag(session, tag)

    def _new_id(self) -> str:
        self._ids_issued += 1
        return f"B{self._ids_issued:04d}"

    def _observe(self, broker_order_id: str) -> OrderUpdate:
        """Next scripted observation (the last one sticks); an unscripted OPEN order fills at once."""
        queue = self.poll_queue.get(broker_order_id)
        if queue:
            update = _unwrap(queue.popleft() if len(queue) > 1 else queue[0])
        else:
            current = self.book[broker_order_id]
            update = current.model_copy(update={"status": OrderStatus.FILLED}) \
                if current.status is OrderStatus.OPEN else current
        self.book[broker_order_id] = update
        return update


class ScriptedRegistry:
    """Stands in for BrokerRegistry: the service only ever calls get(broker_name)."""

    def __init__(self, adapter: ScriptedAdapter) -> None:
        self._adapter = adapter

    def get(self, name: str) -> ScriptedAdapter:
        if name != self._adapter.meta.name:
            raise UnknownBrokerError(f"unknown broker '{name}'")
        return self._adapter

    def describe(self) -> list[BrokerInfo]:
        return [self._adapter.meta.info()]


class RecordingNotifier(Notifier):
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[ExecutionEvent] = []
        self.fail = fail

    async def notify(self, event: ExecutionEvent) -> None:
        self.events.append(event)
        if self.fail:
            raise RuntimeError("sink down")

    def types(self) -> list[str]:
        return [event.type for event in self.events]

    def of(self, type_: str) -> list[ExecutionEvent]:
        return [event for event in self.events if event.type == type_]


def first_time(*lines: tuple[str, int]) -> FirstTimePayload:
    return FirstTimePayload(mode="first_time", lines=[{"symbol": s, "quantity": q} for s, q in lines])


def rebalance(*instructions: RebalanceInstruction) -> RebalancePayload:
    return RebalancePayload(mode="rebalance", instructions=list(instructions))


def sell(symbol: str, quantity: int, **extra: Any) -> RebalanceInstruction:
    return RebalanceInstruction(action="SELL", symbol=symbol, quantity=quantity, **extra)


def buy(symbol: str, quantity: int, **extra: Any) -> RebalanceInstruction:
    return RebalanceInstruction(action="BUY", symbol=symbol, quantity=quantity, **extra)


def rebalance_by(symbol: str, delta: int, **extra: Any) -> RebalanceInstruction:
    return RebalanceInstruction(action="REBALANCE", symbol=symbol, quantity_delta=delta, **extra)


def holding(symbol: str, quantity: int, exchange: str = "NSE") -> Holding:
    return Holding(symbol=symbol, exchange=exchange, quantity=quantity)


def make_report(plan: ExecutionPlan, **overrides: Any) -> ExecutionReport:
    orders = [OrderResult.from_planned(order) for order in plan.orders]
    fields: dict[str, Any] = dict(
        run_id=plan.run_id, idempotency_key=None, broker=plan.broker, mode=plan.mode,
        status=RunStatus.RUNNING, dry_run=False, on_sell_shortfall="halt", plan=plan, orders=orders,
        summary=summarize(orders), started_at=utcnow(),
    )
    return ExecutionReport(**{**fields, **overrides})
