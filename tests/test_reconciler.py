"""reconcile(): expected = before + filled BUYs - filled SELLs; explained diffs are not mismatches."""
from __future__ import annotations

from app.core.models import OrderStatus
from app.execution.reconciler import SETTLES_LATER, reconcile
from tests.fakes import first_time, holding, make_report, rebalance, rebalance_by, sell


def filled(orders, *quantities: int):
    for order, qty in zip(orders, quantities, strict=True):
        order.status = OrderStatus.FILLED if qty == order.quantity else OrderStatus.PARTIALLY_FILLED
        order.filled_qty = qty
    return orders


def test_match(make_plan):
    orders = filled(make_report(make_plan(first_time(("INFY", 10), ("TCS", 5)))).orders, 10, 5)
    result = reconcile([], [holding("INFY", 10), holding("TCS", 5)], orders, same_day_visible=True)
    assert result.status == "MATCH" and result.diffs == []


def test_unexplained_diff_is_mismatch(make_plan):
    orders = filled(make_report(make_plan(first_time(("INFY", 10)))).orders, 10)
    result = reconcile([], [holding("INFY", 7)], orders, same_day_visible=True)
    assert result.status == "MISMATCH"
    assert [(d.symbol, d.expected, d.actual, d.explanation) for d in result.diffs] == [("INFY", 10, 7, None)]


def test_same_day_fill_explained_when_broker_settles_t_plus_1(make_plan):
    orders = filled(make_report(make_plan(first_time(("INFY", 10)))).orders, 10)
    result = reconcile([], [], orders, same_day_visible=False)
    assert result.status == "MATCH"
    assert result.diffs[0].explanation == SETTLES_LATER


def test_unrelated_symbol_is_not_explained_by_settlement(make_plan):
    orders = filled(make_report(make_plan(first_time(("INFY", 10)))).orders, 10)
    result = reconcile([holding("SBIN", 2)], [holding("SBIN", 1)], orders, same_day_visible=False)
    assert result.status == "MISMATCH"
    assert [d.symbol for d in result.diffs if d.explanation is None] == ["SBIN"]


def test_sell_to_zero_removes_the_symbol(make_plan):
    before = [holding("INFY", 4), holding("TCS", 5)]
    plan = make_plan(rebalance(rebalance_by("INFY", -4), sell("TCS", 2)), before)
    orders = filled(make_report(plan).orders, 4, 2)
    result = reconcile(before, [holding("TCS", 3)], orders, same_day_visible=True)
    assert result.status == "MATCH"


def test_partial_fill_counts_what_actually_filled(make_plan):
    orders = filled(make_report(make_plan(first_time(("INFY", 10)))).orders, 4)
    result = reconcile([], [holding("INFY", 4)], orders, same_day_visible=True)
    assert result.status == "MATCH" and "still open" in (result.note or "")


def test_after_none_is_unavailable(make_plan):
    orders = make_report(make_plan(first_time(("INFY", 10)))).orders
    assert reconcile([], None, orders, same_day_visible=True).status == "UNAVAILABLE"
