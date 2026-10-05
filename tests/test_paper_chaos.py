"""Chaos invariant: a full engine run against PaperBroker with every fault on still ends consistent."""
from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

import pytest

from app.brokers.paper import PaperBroker
from app.brokers.ratelimit import BrokerLimiter, RateLimits
from app.core.instruments import InstrumentTable
from app.core.models import (
    ExecutionReport,
    OrderResult,
    RebalanceInstruction,
    RebalancePayload,
    RunStatus,
    utcnow,
)
from app.execution import engine as engine_mod
from app.execution import planner
from app.execution import report as report_mod
from app.execution import store as store_mod
from app.notifications.base import Notifier

pytestmark = pytest.mark.asyncio
TABLE = InstrumentTable.load(Path(__file__).resolve().parents[1] / "data" / "instruments.json")
SYMBOLS = sorted(s for s, _ in TABLE._by_key if s != "REJECTME")
HELD, BOUGHT = SYMBOLS[:20], SYMBOLS[20:60]
FAULTS = {"latency_ms": "0-0", "reject_rate": "0.2", "rate_limit_every_n": "7", "ambiguous_rate": "0.1",
          "ambiguous_placed": "true", "partial_fill_rate": "0.1", "fill_after_polls": "2", "seed": "7"}


class Recorder(Notifier):
    def __init__(self) -> None:
        self.events = []

    async def notify(self, event) -> None:
        self.events.append(event)


async def run_chaos(**knobs: str):
    broker = PaperBroker(None, TABLE)
    broker.limiter = BrokerLimiter(RateLimits(1000, 100000, 1000), max_inflight=64)  # bypass paper's 10/s
    session = await broker.complete_login({**FAULTS, **knobs, "seed_holdings": ",".join(f"{s}:10" for s in HELD)})
    holdings = await broker.get_holdings(session)
    instructions = [RebalanceInstruction(symbol=s, action="SELL", quantity=10) for s in HELD]
    instructions += [RebalanceInstruction(symbol=s, action="BUY", quantity=5) for s in BOUGHT]
    run_id = uuid4().hex
    plan = planner.build_plan(run_id, session, RebalancePayload(mode="rebalance", instructions=instructions),
                              holdings, TABLE)
    orders = [OrderResult.from_planned(o) for o in plan.orders]
    report = ExecutionReport(run_id=run_id, idempotency_key=None, broker="paper", mode="rebalance",
                             status=RunStatus.RUNNING, dry_run=False, on_sell_shortfall="continue", plan=plan,
                             orders=orders, summary=report_mod.summarize(orders), started_at=utcnow())
    config = engine_mod.EngineConfig(place_max_attempts=3, retry_base_delay_s=0.001, retry_max_delay_s=0.002,
                                     poll_interval_s=0.005, poll_timeout_s=0.3, ambiguous_lookup_attempts=3,
                                     ambiguous_lookup_interval_s=0.005, market_hours_warn=False)
    recorder = Recorder()
    engine = engine_mod.ExecutionEngine(store_mod.InMemoryRunRepository(), recorder, config)
    started = time.monotonic()
    result = await engine.run(report, session, broker)
    return result, broker.accounts[session.session_id], recorder, time.monotonic() - started


def check_invariants(result, account, recorder, elapsed) -> None:
    assert len(result.orders) == 60 and result.summary.total == 60
    assert all(o.status.is_terminal for o in result.orders), [o.status for o in result.orders]
    assert account.duplicate_tags == 0
    assert len(account.orders) <= 60
    assert result.status == report_mod.derive_run_status(result.orders)
    assert result.status in (RunStatus.PARTIALLY_FAILED, RunStatus.COMPLETED)
    assert result.reconciliation is not None and result.reconciliation.status == "MATCH", result.reconciliation
    assert sum(1 for e in recorder.events if e.type == "run.completed") == 1
    assert elapsed < 5.0


async def test_chaos_with_ambiguous_orders_actually_placed():
    result, account, recorder, elapsed = await run_chaos()
    check_invariants(result, account, recorder, elapsed)
    statuses = {o.status for o in result.orders}
    assert "FILLED" in statuses and "REJECTED" in statuses  # the faults really fired


async def test_chaos_with_ambiguous_orders_dropped():
    result, account, recorder, elapsed = await run_chaos(ambiguous_placed="false")
    check_invariants(result, account, recorder, elapsed)
    assert any(o.status == "UNKNOWN" for o in result.orders)
