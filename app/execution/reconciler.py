"""Advisory post-run check: do the broker's holdings match what the fills say they should be?"""
from __future__ import annotations

from app.core.models import Holding, OrderResult, OrderSide, OrderStatus, ReconciliationResult, SymbolDiff

Key = tuple[str, str]
SETTLES_LATER = "filled today; settles T+1"
LEFT_OPEN = frozenset({OrderStatus.PARTIALLY_FILLED, OrderStatus.TIMED_OUT, OrderStatus.UNKNOWN})


def reconcile(before: list[Holding], after: list[Holding] | None, orders: list[OrderResult],
              *, same_day_visible: bool) -> ReconciliationResult:
    if after is None:
        return ReconciliationResult(status="UNAVAILABLE", note="holdings could not be re-fetched")
    expected = _quantities(before)
    filled_today: set[Key] = set()
    for order in orders:
        if order.filled_qty <= 0:
            continue
        key = (order.symbol, order.exchange)
        signed = order.filled_qty if order.side is OrderSide.BUY else -order.filled_qty
        expected[key] = expected.get(key, 0) + signed
        filled_today.add(key)
    actual = _quantities(after)
    # real brokers show today's fills in positions, not holdings, until T+1
    diffs = [_diff(key, expected.get(key, 0), actual.get(key, 0),
                   explained=key in filled_today and not same_day_visible)
             for key in sorted(expected.keys() | actual.keys()) if expected.get(key, 0) != actual.get(key, 0)]
    status = "MATCH" if all(diff.explanation for diff in diffs) else "MISMATCH"
    return ReconciliationResult(status=status, diffs=diffs, note=_note(orders))


def _quantities(holdings: list[Holding]) -> dict[Key, int]:
    return {(h.symbol.upper(), h.exchange): h.quantity for h in holdings if h.quantity > 0}


def _diff(key: Key, expected: int, actual: int, *, explained: bool) -> SymbolDiff:
    symbol, exchange = key
    return SymbolDiff(symbol=symbol, exchange=exchange, expected=expected, actual=actual,
                      explanation=SETTLES_LATER if explained else None)


def _note(orders: list[OrderResult]) -> str | None:
    unsettled = sum(order.status in LEFT_OPEN for order in orders)
    if not unsettled:
        return None
    return f"advisory: {unsettled} order(s) are still open or unresolved at the broker and may fill later"
