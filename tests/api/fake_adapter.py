"""A deterministic in-memory broker for the API tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from secrets import token_urlsafe
from typing import Any

import httpx
from pydantic import SecretStr

from app.brokers.base import (
    AuthError,
    BrokerAdapter,
    BrokerMeta,
    BrokerUnavailableError,
    OrderRejectedError,
    RateLimits,
)
from app.core.instruments import InstrumentTable
from app.core.models import (
    BrokerSession,
    Holding,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderUpdate,
    utcnow,
)


@dataclass
class FakeOrder:
    broker_order_id: str
    tag: str
    symbol: str
    exchange: str
    side: OrderSide
    quantity: int
    status: OrderStatus = OrderStatus.OPEN
    filled_qty: int = 0
    polls_seen: int = 0


@dataclass
class FakeAccount:
    fill_after_polls: int = 1
    holdings: dict[tuple[str, str], Holding] = field(default_factory=dict)
    orders: dict[str, FakeOrder] = field(default_factory=dict)


class FakeAdapter(BrokerAdapter):
    meta = BrokerMeta(
        name="fake", display_name="Fake (tests only)", required_credentials=("api_key",),
        optional_credentials=("seed_holdings", "fill_after_polls"), limits=RateLimits(100, 6000, 100),
        live_tested=True, holdings_show_same_day_fills=True, credential_help="api_key: anything but 'bad'",
    )
    STATUS_MAP = {s.value.lower(): s for s in (OrderStatus.OPEN, OrderStatus.FILLED, OrderStatus.REJECTED)}

    def __init__(self, http: httpx.AsyncClient, instruments: InstrumentTable,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(http, instruments, config)
        self.accounts: dict[str, FakeAccount] = {}
        self.place_calls = 0
        self.hold_fills = False  # freezes fills for WS tests

    def login_url(self, credentials: dict[str, str]) -> str | None:
        key = credentials.get("api_key")
        return f"https://fake.example/login?api_key={key}" if key else None

    async def complete_login(self, credentials: dict[str, str]) -> BrokerSession:
        if credentials.get("api_key") == "bad":
            raise AuthError("invalid api_key")
        account = FakeAccount(fill_after_polls=int(credentials.get("fill_after_polls", 1)))
        for part in credentials.get("seed_holdings", "").split(","):
            if not part.strip():
                continue
            symbol, _, qty = part.partition(":")
            inst = self.instruments.get(symbol.strip().upper())
            account.holdings[inst.key] = Holding(symbol=inst.symbol, exchange=inst.exchange,
                                                 quantity=int(qty), isin=inst.isin)
        session_id = token_urlsafe(16)
        self.accounts[session_id] = account
        return BrokerSession(session_id=session_id, broker="fake", user_id="fake-user",
                             access_token=SecretStr("fake-access-token"), expires_at=utcnow() + timedelta(hours=8))

    def _account(self, session: BrokerSession) -> FakeAccount:
        try:
            return self.accounts[session.session_id]
        except KeyError:
            raise AuthError("unknown session") from None

    async def get_holdings(self, session: BrokerSession) -> list[Holding]:
        account = self._account(session)
        return sorted((h for h in account.holdings.values() if h.quantity > 0), key=lambda h: h.symbol)

    async def get_order(self, session: BrokerSession, broker_order_id: str) -> OrderUpdate:
        account = self._account(session)
        order = account.orders.get(broker_order_id)
        if order is None:
            raise BrokerUnavailableError("unknown order")
        self._advance(account, order)
        return self._update(order)

    async def list_orders(self, session: BrokerSession) -> list[OrderUpdate]:
        account = self._account(session)
        for order in account.orders.values():
            self._advance(account, order)
        return [self._update(o) for o in account.orders.values()]

    async def place_order(self, session: BrokerSession, order: OrderRequest) -> str:
        account = self._account(session)
        self.place_calls += 1
        if order.symbol == "REJECTME":
            raise OrderRejectedError("simulated rejection: RMS")
        held = account.holdings.get((order.symbol, order.exchange))
        if order.side is OrderSide.SELL and (held is None or held.quantity < order.quantity):
            raise OrderRejectedError("Holdings not available for sell")
        broker_order_id = f"F{len(account.orders) + 1:06d}"
        account.orders[broker_order_id] = FakeOrder(broker_order_id, order.tag, order.symbol, order.exchange,
                                                    order.side, order.quantity)
        return broker_order_id

    def _advance(self, account: FakeAccount, order: FakeOrder) -> None:
        if order.status is not OrderStatus.OPEN or self.hold_fills:
            return
        order.polls_seen += 1
        if order.polls_seen < account.fill_after_polls:
            return
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        key = (order.symbol, order.exchange)
        held = account.holdings.get(key)
        if order.side is OrderSide.BUY:
            inst = self.instruments.get(order.symbol, order.exchange)
            quantity = (held.quantity if held else 0) + order.quantity
            account.holdings[key] = Holding(symbol=inst.symbol, exchange=inst.exchange,
                                            quantity=quantity, isin=inst.isin)
        elif held is not None:
            remaining = held.quantity - order.quantity
            if remaining > 0:
                account.holdings[key] = held.model_copy(update={"quantity": remaining})
            else:
                del account.holdings[key]

    @staticmethod
    def _update(order: FakeOrder) -> OrderUpdate:
        return OrderUpdate(broker_order_id=order.broker_order_id, status=order.status, filled_qty=order.filled_qty,
                           average_price=Decimal(100) if order.status is OrderStatus.FILLED else None,
                           tag=order.tag, raw_status=order.status.value)
