"""Polls OPEN orders until the broker reports them terminal or the phase's time budget runs out."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.brokers.base import AuthError, BrokerAdapter, BrokerError
from app.core.models import BrokerSession, ErrorCode, OrderResult, OrderStatus, OrderUpdate, utcnow

log = logging.getLogger(__name__)

BOOK_THRESHOLD = 3  # above this, one list_orders() call
LEFT_OPEN_MESSAGE = "still open at broker; not cancelled"
BROKER_TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED})

OnUpdate = Callable[[OrderResult], Awaitable[None]]


async def poll_until_terminal(adapter: BrokerAdapter, session: BrokerSession, orders: list[OrderResult], *,
                              interval_s: float, timeout_s: float, on_update: OnUpdate) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    open_orders = _still_open(orders)
    while open_orders:
        try:
            updates = await _read_open_orders(adapter, session, open_orders)
        except AuthError as exc:
            message = f"auth failed while polling ({exc.message}); {LEFT_OPEN_MESSAGE}"
            await _close_out(open_orders, ErrorCode.AUTH, message, on_update)
            return
        for order in open_orders:
            update = updates.get(order.broker_order_id or "")
            if update is None:
                continue
            if apply_update(order, update):
                await on_update(order)
        open_orders = _still_open(open_orders)
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(interval_s, remaining))
    await _close_out(open_orders, ErrorCode.POLL_TIMEOUT, LEFT_OPEN_MESSAGE, on_update)


def apply_update(order: OrderResult, update: OrderUpdate) -> bool:
    """Copy what the broker reports onto the order. Returns True when status or filled quantity changed."""
    before = (order.status, order.filled_qty)
    order.broker_order_id = update.broker_order_id
    order.broker_status_raw = update.raw_status
    order.average_price = update.average_price
    if update.status in BROKER_TERMINAL:
        order.status = update.status
        order.filled_qty = order.quantity if update.status is OrderStatus.FILLED else update.filled_qty
        order.finalized_at = utcnow()
        if update.status is OrderStatus.REJECTED:
            order.error_code = ErrorCode.REJECTED
            order.error_message = update.message or "rejected by broker"
    else:  # PENDING/OPEN: still working
        order.status = OrderStatus.OPEN
        order.filled_qty = update.filled_qty
    return (order.status, order.filled_qty) != before


def mark_terminal(order: OrderResult, status: OrderStatus, code: ErrorCode, message: str) -> None:
    """Set an engine-side terminal state (FAILED, TIMED_OUT, UNKNOWN, SKIPPED) with its reason."""
    order.status, order.error_code, order.error_message, order.finalized_at = status, code, message, utcnow()


def _still_open(orders: list[OrderResult]) -> list[OrderResult]:
    return [order for order in orders if order.status is OrderStatus.OPEN]


async def _read_open_orders(adapter: BrokerAdapter, session: BrokerSession,
                            open_orders: list[OrderResult]) -> dict[str, OrderUpdate]:
    """Broker state keyed by broker_order_id. AuthError propagates; any other broker error skips just the
    read that failed, so one unreadable order never starves its siblings."""
    if len(open_orders) > BOOK_THRESHOLD:
        try:
            async with adapter.limiter.reads():
                return {u.broker_order_id: u for u in await adapter.list_orders(session)}
        except AuthError:
            raise
        except BrokerError as exc:
            log.warning("poll.read_error", extra={"error": exc.message})
            return {}
    updates: dict[str, OrderUpdate] = {}
    for order in open_orders:
        broker_order_id = order.broker_order_id or ""
        try:
            async with adapter.limiter.reads():
                updates[broker_order_id] = await adapter.get_order(session, broker_order_id)
        except AuthError:
            raise
        except BrokerError as exc:
            log.warning("poll.read_error", extra={"error": exc.message, "tag": order.tag})
    return updates


async def _close_out(orders: list[OrderResult], code: ErrorCode, message: str, on_update: OnUpdate) -> None:
    for order in orders:
        status = OrderStatus.PARTIALLY_FILLED if order.filled_qty > 0 else OrderStatus.TIMED_OUT
        mark_terminal(order, status, code, message)
        await on_update(order)
