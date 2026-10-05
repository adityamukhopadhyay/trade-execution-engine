"""ExecutionEngine against ScriptedAdapter: phases, isolation, retries, ambiguity, polling, the sell gate."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest

from app.brokers.base import (
    AmbiguousOutcomeError,
    AuthError,
    BrokerUnavailableError,
    OrderRejectedError,
    RateLimitError,
)
from app.core.context import order_id_var, run_id_var
from app.core.models import ErrorCode, ExecutionReport, OrderStatus, OrderUpdate, Phase, RunStatus
from app.execution.engine import ExecutionEngine
from tests.fakes import (
    RecordingNotifier,
    buy,
    first_time,
    holding,
    make_report,
    rebalance,
    rebalance_by,
    sell,
)

pytestmark = pytest.mark.asyncio
S = OrderStatus
HELD = [holding("INFY", 10), holding("TCS", 5)]


@pytest.fixture
def run(engine, adapter, session):
    async def _run(report: ExecutionReport) -> ExecutionReport:
        return await engine.run(report, session, adapter)
    return _run


def tag_of(report: ExecutionReport, symbol: str) -> str:
    return next(order.tag for order in report.orders if order.symbol == symbol)


def by_symbol(report: ExecutionReport):
    return {order.symbol: order for order in report.orders}


def update(broker_order_id: str, status: OrderStatus, **fields) -> OrderUpdate:
    return OrderUpdate(broker_order_id=broker_order_id, status=status, **fields)


async def test_happy_path_events_statuses_and_reconciliation(run, adapter, notifier, make_plan):
    adapter.holdings = HELD
    report = await run(make_report(make_plan(first_time(("INFY", 10), ("TCS", 5)))))
    assert report.status is RunStatus.COMPLETED and report.finished_at is not None
    assert all(o.status is S.FILLED and o.filled_qty == o.quantity and o.finalized_at for o in report.orders)
    types = notifier.types()
    assert types[:2] == ["run.started", "run.phase"] and types[-1] == "run.completed"
    assert notifier.events[1].phase is Phase.BUY
    assert set(types[2:-1]) == {"order.updated"} and len(notifier.of("order.updated")) == 4
    assert report.summary.model_dump(include={"total", "filled"}) == {"total": 2, "filled": 2}
    assert report.reconciliation is not None and report.reconciliation.status == "MATCH"


async def test_one_sync_reject_is_isolated(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1), ("TCS", 1), ("SBIN", 1))))
    adapter.script_place(tag_of(report, "TCS"), OrderRejectedError("insufficient funds"))
    await run(report)
    orders = by_symbol(report)
    assert (orders["TCS"].status, orders["TCS"].error_code) == (S.REJECTED, ErrorCode.REJECTED)
    assert orders["TCS"].error_message == "insufficient funds" and orders["TCS"].broker_order_id is None
    assert orders["INFY"].status is S.FILLED and orders["SBIN"].status is S.FILLED
    assert report.status is RunStatus.PARTIALLY_FAILED


async def test_unexpected_exception_is_internal_and_run_continues(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1), ("TCS", 1))))
    adapter.script_place(tag_of(report, "INFY"), KeyError("adapter bug"))
    await run(report)
    orders = by_symbol(report)
    assert (orders["INFY"].status, orders["INFY"].error_code) == (S.FAILED, ErrorCode.INTERNAL)
    assert "KeyError" in (orders["INFY"].error_message or "")
    assert orders["TCS"].status is S.FILLED and report.status is RunStatus.PARTIALLY_FAILED


async def test_sells_finish_before_any_buy_is_placed(run, adapter, notifier, make_plan):
    portfolio = rebalance(sell("TCS", 5), rebalance_by("INFY", -4), buy("HDFCBANK", 6),
                          rebalance_by("RELIANCE", 2))
    report = await run(make_report(make_plan(portfolio, HELD)))
    sells = [o for o in report.orders if o.phase is Phase.SELL]
    buys = [o for o in report.orders if o.phase is Phase.BUY]
    assert len(sells) == 2 and len(buys) == 2 and report.status is RunStatus.COMPLETED
    assert max(o.submitted_at for o in sells) <= min(o.submitted_at for o in buys)
    sell_tags = {o.tag for o in sells}
    first_buy = next(i for i, (name, arg) in enumerate(adapter.calls)
                     if name == "place_order" and arg not in sell_tags)
    before_buys = adapter.calls[:first_buy]
    assert [arg for name, arg in before_buys if name == "place_order"] == [o.tag for o in sells]
    assert any(name == "get_order" for name, _ in before_buys)
    assert [e.phase for e in notifier.of("run.phase")] == [Phase.SELL, Phase.BUY]


async def test_gate_halt_skips_every_buy(run, adapter, notifier, make_plan):
    portfolio = rebalance(sell("TCS", 5), sell("INFY", 4), buy("HDFCBANK", 6), buy("SBIN", 1))
    report = make_report(make_plan(portfolio, HELD))
    adapter.script_place(tag_of(report, "INFY"), OrderRejectedError("holdings not available"))
    await run(report)
    buys = [o for o in report.orders if o.phase is Phase.BUY]
    assert all(o.status is S.SKIPPED and o.error_code is ErrorCode.SELL_PHASE_HALTED for o in buys)
    assert all("INFY REJECTED" in (o.error_message or "") for o in buys)
    assert set(adapter.placed_tags()).isdisjoint({o.tag for o in buys})
    assert [e.phase for e in notifier.of("run.phase")] == [Phase.SELL]
    assert report.status is RunStatus.PARTIALLY_FAILED


async def test_gate_continue_places_buys_and_warns(run, adapter, make_plan):
    portfolio = rebalance(sell("TCS", 5), sell("INFY", 4), buy("HDFCBANK", 6))
    report = make_report(make_plan(portfolio, HELD), on_sell_shortfall="continue")
    adapter.script_place(tag_of(report, "INFY"), OrderRejectedError("holdings not available"))
    await run(report)
    assert by_symbol(report)["HDFCBANK"].status is S.FILLED
    assert any("SELL shortfall" in warning for warning in report.plan.warnings)


async def test_rate_limit_retried_then_placed(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1))))
    adapter.script_place(report.orders[0].tag, RateLimitError("429"), RateLimitError("429"), "B-OK")
    await run(report)
    order = report.orders[0]
    assert (order.status, order.attempts, order.broker_order_id) == (S.FILLED, 3, "B-OK")
    assert order.error_code is None
    assert adapter.count("place_order") == 3


async def test_unavailable_exhausts_attempts(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1))))
    adapter.script_place(report.orders[0].tag, *[BrokerUnavailableError("connect refused")] * 3)
    await run(report)
    order = report.orders[0]
    assert (order.status, order.error_code, order.attempts) == (S.FAILED, ErrorCode.BROKER_UNAVAILABLE, 3)
    assert order.error_message == "connect refused" and report.status is RunStatus.FAILED


async def test_retry_after_is_honoured_up_to_the_cap(adapter, session, runs, notifier, fast_config, make_plan,
                                                     monkeypatch):
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay: float) -> None:
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    engine = ExecutionEngine(runs, notifier, replace(fast_config, retry_max_delay_s=1.0))
    report = make_report(make_plan(first_time(("INFY", 1))))
    adapter.script_place(report.orders[0].tag, RateLimitError("429", retry_after=0.7),
                         RateLimitError("429", retry_after=3600), "B-OK")
    await engine.run(report, session, adapter)
    assert report.orders[0].status is S.FILLED and 0.7 in slept and max(slept) == 1.0


async def test_auth_error_on_place_is_not_retried(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1))))
    adapter.script_place(report.orders[0].tag, AuthError("token expired"))
    await run(report)
    order = report.orders[0]
    assert (order.status, order.error_code, order.attempts) == (S.FAILED, ErrorCode.AUTH, 1)
    assert adapter.count("place_order") == 1


async def test_ambiguous_located_open_then_filled(run, adapter, notifier, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1))))
    tag = report.orders[0].tag
    adapter.script_place(tag, AmbiguousOutcomeError("read timeout after send"))
    adapter.seed_book("B-AMB", tag)
    adapter.script_poll("B-AMB", update("B-AMB", S.OPEN, tag=tag), update("B-AMB", S.FILLED, tag=tag))
    await run(report)
    order = report.orders[0]
    assert (order.status, order.broker_order_id, order.error_code) == (S.FILLED, "B-AMB", None)
    assert adapter.count("place_order") == 1 and adapter.count("find_order_by_tag") == 1
    assert [e.order.status for e in notifier.of("order.updated")] == [S.AMBIGUOUS, S.OPEN, S.FILLED]


async def test_ambiguous_located_already_filled(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1))))
    tag = report.orders[0].tag
    adapter.script_place(tag, AmbiguousOutcomeError("502 after send"))
    adapter.seed_book("B-AMB", tag, S.FILLED)
    await run(report)
    assert report.orders[0].status is S.FILLED and report.status is RunStatus.COMPLETED
    assert adapter.count("place_order") == 1 and adapter.count("get_order") == 0


async def test_ambiguous_lookup_errors_are_retried_within_budget(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1))))
    tag = report.orders[0].tag
    adapter.script_place(tag, AmbiguousOutcomeError("reset after send"))
    adapter.seed_book("B-AMB", tag)
    adapter.script_poll("B-AMB", BrokerUnavailableError("book down"), update("B-AMB", S.FILLED, tag=tag))
    await run(report)
    assert report.orders[0].status is S.FILLED and adapter.count("find_order_by_tag") == 2


async def test_ambiguous_not_located_becomes_unknown(run, adapter, make_plan, fast_config):
    report = make_report(make_plan(first_time(("INFY", 1), ("TCS", 1))))
    adapter.script_place(tag_of(report, "INFY"), AmbiguousOutcomeError("read timeout after send"))
    await run(report)
    order = by_symbol(report)["INFY"]
    assert (order.status, order.error_code) == (S.UNKNOWN, ErrorCode.AMBIGUOUS_UNRESOLVED)
    assert "NOT resent" in (order.error_message or "")
    assert adapter.placed_tags().count(order.tag) == 1
    assert adapter.count("find_order_by_tag") == fast_config.ambiguous_lookup_attempts
    assert by_symbol(report)["TCS"].status is S.FILLED and report.status is RunStatus.PARTIALLY_FAILED


async def test_poll_deadline_without_fills_times_out(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 10))))
    adapter.script_place(report.orders[0].tag, "B1")
    adapter.script_poll("B1", update("B1", S.OPEN))  # sticks: never fills
    await run(report)
    order = report.orders[0]
    assert (order.status, order.error_code) == (S.TIMED_OUT, ErrorCode.POLL_TIMEOUT)
    assert "not cancelled" in (order.error_message or "") and adapter.count("get_order") >= 2
    assert report.status is RunStatus.FAILED


async def test_poll_deadline_with_partial_fill(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 10))))
    adapter.script_place(report.orders[0].tag, "B1")
    adapter.script_poll("B1", update("B1", S.OPEN, filled_qty=4))
    await run(report)
    order = report.orders[0]
    assert (order.status, order.filled_qty) == (S.PARTIALLY_FILLED, 4)
    assert order.error_code is ErrorCode.POLL_TIMEOUT
    assert report.status is RunStatus.PARTIALLY_FAILED and report.summary.partially_filled == 1


async def test_accepted_then_rejected_on_poll(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1))))
    adapter.script_place(report.orders[0].tag, "B1")
    adapter.script_poll("B1", update("B1", S.OPEN), update("B1", S.REJECTED, message="RMS: margin shortfall"))
    await run(report)
    order = report.orders[0]
    assert (order.status, order.error_code, order.error_message) == (S.REJECTED, ErrorCode.REJECTED,
                                                                     "RMS: margin shortfall")


async def test_read_error_during_polling_skips_the_tick(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1))))
    adapter.script_place(report.orders[0].tag, "B1")
    adapter.script_poll("B1", BrokerUnavailableError("502"), update("B1", S.FILLED))
    await run(report)
    assert report.orders[0].status is S.FILLED and adapter.count("get_order") == 2


async def test_one_unreadable_order_does_not_starve_its_siblings(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1), ("TCS", 1), ("SBIN", 1))))
    adapter.script_place(tag_of(report, "INFY"), "B1")
    adapter.script_poll("B1", BrokerUnavailableError("order B1 not found"))
    await run(report)
    orders = by_symbol(report)
    assert (orders["INFY"].status, orders["INFY"].error_code) == (S.TIMED_OUT, ErrorCode.POLL_TIMEOUT)
    assert orders["TCS"].status is S.FILLED and orders["SBIN"].status is S.FILLED
    assert report.status is RunStatus.PARTIALLY_FAILED


async def test_auth_error_during_polling_stops_the_phase(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1), ("TCS", 1))))
    adapter.script_place(tag_of(report, "INFY"), "B1")
    adapter.script_poll("B1", AuthError("token expired"))
    await run(report)
    order = by_symbol(report)["INFY"]
    assert (order.status, order.error_code) == (S.TIMED_OUT, ErrorCode.AUTH)
    assert adapter.count("get_order") == 1


async def test_many_open_orders_poll_the_order_book(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1), ("TCS", 1), ("SBIN", 1), ("WIPRO", 1))))
    await run(report)
    assert adapter.count("list_orders") >= 1 and adapter.count("get_order") == 0
    assert report.status is RunStatus.COMPLETED


async def test_few_open_orders_poll_individually(run, adapter, make_plan):
    report = make_report(make_plan(first_time(("INFY", 1), ("TCS", 1), ("SBIN", 1))))
    await run(report)
    assert adapter.count("get_order") == 3 and adapter.count("list_orders") == 0


async def test_reconciliation_unavailable_when_holdings_refetch_fails(run, adapter, make_plan):
    adapter.holdings_queue.append(BrokerUnavailableError("holdings down"))
    report = await run(make_report(make_plan(first_time(("INFY", 1)))))
    assert report.reconciliation is not None and report.reconciliation.status == "UNAVAILABLE"
    assert report.status is RunStatus.COMPLETED


async def test_reconciliation_mismatch_is_advisory(run, adapter, make_plan):
    adapter.holdings = []
    report = await run(make_report(make_plan(first_time(("INFY", 1)))))
    assert report.reconciliation is not None and report.reconciliation.status == "MISMATCH"
    assert report.status is RunStatus.COMPLETED


async def test_report_saved_live_and_context_vars_set(run, adapter, runs, notifier, make_plan, caplog):
    caplog.set_level(logging.INFO, logger="app.execution")
    stamped: list[str | None] = []
    caplog.handler.addFilter(lambda record: stamped.append(run_id_var.get()) or True)
    report = make_report(make_plan(first_time(("INFY", 1))))
    await run(report)
    assert runs.get(report.run_id) is report and report.status is RunStatus.COMPLETED
    assert adapter.contexts == [(report.run_id, report.orders[0].tag)]
    assert order_id_var.get() is None
    assert notifier.of("run.started")[0].report.status is RunStatus.RUNNING  # events carry snapshots
    assert report.run_id in stamped and "order.updated" in caplog.text


async def test_broken_notifier_never_changes_the_run(adapter, session, runs, fast_config, make_plan):
    engine = ExecutionEngine(runs, RecordingNotifier(fail=True), fast_config)
    report = await engine.run(make_report(make_plan(first_time(("INFY", 1)))), session, adapter)
    assert report.status is RunStatus.COMPLETED


async def test_engine_crash_still_finalizes(engine, run, notifier, make_plan, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(engine, "_run_phases", boom)
    report = await run(make_report(make_plan(first_time(("INFY", 1)))))
    assert report.orders[0].status is S.FAILED and report.orders[0].error_code is ErrorCode.INTERNAL
    assert report.status is RunStatus.FAILED and notifier.types()[-1] == "run.completed"


async def test_engine_crash_after_placement_marks_unknown(run, make_plan, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr("app.execution.engine.poll_until_terminal", boom)
    report = await run(make_report(make_plan(first_time(("INFY", 1)))))
    order = report.orders[0]
    assert (order.status, order.error_code) == (S.UNKNOWN, ErrorCode.INTERNAL) and order.broker_order_id
    assert "verify at broker" in (order.error_message or "") and report.status is RunStatus.FAILED
