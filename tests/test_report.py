"""derive_run_status() truth table and summarize() counts."""
from __future__ import annotations

import pytest

from app.core.models import OrderStatus, RunStatus
from app.execution.report import derive_run_status, summarize
from tests.fakes import first_time, make_report

S = OrderStatus


def orders_with(make_plan, *statuses: OrderStatus):
    symbols = ["INFY", "TCS", "SBIN", "WIPRO"][:len(statuses)]
    plan = make_plan(first_time(*((symbol, 1) for symbol in symbols)))
    report = make_report(plan)
    for order, status in zip(report.orders, statuses, strict=True):
        order.status = status
        if status in (S.FILLED, S.PARTIALLY_FILLED):
            order.filled_qty = 1
    return report.orders


@pytest.mark.parametrize("statuses, expected", [
    ((S.FILLED, S.FILLED), RunStatus.COMPLETED),
    ((S.REJECTED, S.FAILED), RunStatus.FAILED),
    ((S.FILLED, S.REJECTED), RunStatus.PARTIALLY_FAILED),
    ((S.PARTIALLY_FILLED, S.TIMED_OUT), RunStatus.PARTIALLY_FAILED),
    ((S.PARTIALLY_FILLED,), RunStatus.PARTIALLY_FAILED),
    ((S.SKIPPED, S.SKIPPED), RunStatus.FAILED),
    ((S.UNKNOWN, S.TIMED_OUT, S.CANCELLED), RunStatus.FAILED),
])
def test_derive_run_status(make_plan, statuses, expected):
    assert derive_run_status(orders_with(make_plan, *statuses)) is expected


def test_cancelled_order_with_fills_is_a_partial_failure(make_plan):
    orders = orders_with(make_plan, S.CANCELLED, S.REJECTED)
    orders[0].filled_qty = 6
    assert derive_run_status(orders) is RunStatus.PARTIALLY_FAILED


def test_summarize_counts_every_status(make_plan):
    orders = orders_with(make_plan, S.FILLED, S.PARTIALLY_FILLED, S.REJECTED, S.SKIPPED)
    summary = summarize(orders)
    assert summary.model_dump() == {"total": 4, "filled": 1, "partially_filled": 1, "rejected": 1,
                                    "cancelled": 0, "failed": 0, "timed_out": 0, "unknown": 0, "skipped": 1}


def test_summarize_ignores_non_terminal(make_plan):
    orders = orders_with(make_plan, S.PENDING, S.OPEN, S.AMBIGUOUS)
    assert summarize(orders).model_dump() == {"total": 3, "filled": 0, "partially_filled": 0, "rejected": 0,
                                              "cancelled": 0, "failed": 0, "timed_out": 0, "unknown": 0,
                                              "skipped": 0}
