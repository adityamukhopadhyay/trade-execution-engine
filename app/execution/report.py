"""Roll-ups over a run's OrderResults: per-status counts and the run's final verdict."""
from __future__ import annotations

from collections import Counter

from app.core.models import OrderResult, OrderStatus, RunStatus, RunSummary

SUMMARY_FIELD: dict[OrderStatus, str] = {
    OrderStatus.FILLED: "filled",
    OrderStatus.PARTIALLY_FILLED: "partially_filled",
    OrderStatus.REJECTED: "rejected",
    OrderStatus.CANCELLED: "cancelled",
    OrderStatus.FAILED: "failed",
    OrderStatus.TIMED_OUT: "timed_out",
    OrderStatus.UNKNOWN: "unknown",
    OrderStatus.SKIPPED: "skipped",
}
PARTIAL_SUCCESS = frozenset({OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED})


def summarize(orders: list[OrderResult]) -> RunSummary:
    counts = Counter(SUMMARY_FIELD.get(order.status) for order in orders)  # non-terminal -> None
    return RunSummary(total=len(orders), **{field: counts[field] for field in SUMMARY_FIELD.values()})


def derive_run_status(orders: list[OrderResult]) -> RunStatus:
    """COMPLETED when every order filled; FAILED when nothing filled at all; PARTIALLY_FAILED in between."""
    if all(order.status is OrderStatus.FILLED for order in orders):
        return RunStatus.COMPLETED
    if any(order.status in PARTIAL_SUCCESS or order.filled_qty > 0 for order in orders):
        return RunStatus.PARTIALLY_FAILED
    return RunStatus.FAILED
