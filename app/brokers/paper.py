"""Paper broker: an in-memory simulated exchange with injectable faults."""
from __future__ import annotations

import asyncio
import random
import zlib
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from secrets import token_urlsafe
from typing import Any

from pydantic import SecretStr

from app.brokers.base import (
    AmbiguousOutcomeError,
    AuthError,
    BrokerAdapter,
    BrokerMeta,
    BrokerUnavailableError,
    OrderRejectedError,
    RateLimitError,
    RateLimits,
    SymbolNotFoundError,
)
from app.core.models import BrokerSession, Holding, OrderRequest, OrderSide, OrderStatus, OrderUpdate, utcnow


@dataclass
class PaperFaults:
    reject_rate: float = 0.0          # P(sync OrderRejectedError) per place
    latency_ms: tuple[int, int] = (20, 120)
    rate_limit_every_n: int = 0       # 0 = off
    ambiguous_rate: float = 0.0       # P(AmbiguousOutcomeError) per place
    ambiguous_placed: bool = True     # was the order recorded anyway?
    partial_fill_rate: float = 0.0    # P(half fill, then stays OPEN)
    fill_after_polls: int = 1         # fills on the n-th observation
    seed: int = 42


def _as_range(value: Any) -> tuple[int, int]:  # "20-120" -> (20, 120)
    lo, _, hi = value.partition("-") if isinstance(value, str) else (value[0], None, value[1])
    return int(lo), int(hi or lo)


PARSERS = {"reject_rate": float, "ambiguous_rate": float, "partial_fill_rate": float, "rate_limit_every_n": int,
           "fill_after_polls": int, "seed": int, "latency_ms": _as_range,
           "ambiguous_placed": lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes")}


def parse_faults(config: dict[str, Any], credentials: dict[str, str]) -> PaperFaults:
    raw = dict(config.get("faults") or {})
    raw.update({k: v for k, v in credentials.items() if k in PARSERS and v not in (None, "")})
    return PaperFaults(**{k: PARSERS[k](v) for k, v in raw.items() if k in PARSERS})


def price(symbol: str) -> Decimal:
    return Decimal(100 + zlib.crc32(symbol.encode()) % 2400)


def _update(order: PaperOrder) -> OrderUpdate:
    return OrderUpdate(broker_order_id=order.broker_order_id, status=order.status, filled_qty=order.filled_qty,
                       average_price=price(order.symbol) if order.filled_qty else None, tag=order.tag,
                       message="simulated RMS rejection" if order.status is OrderStatus.REJECTED else None,
                       raw_status=order.status.value)


@dataclass
class PaperOrder:
    broker_order_id: str
    tag: str
    symbol: str
    exchange: str
    side: OrderSide
    quantity: int
    filled_qty: int = 0
    status: OrderStatus = OrderStatus.OPEN
    polls_seen: int = 0
    partial: bool = False
    reject_on_poll: bool = False   # REJECTME: rejected on first poll


@dataclass
class PaperAccount:
    faults: PaperFaults
    rng: random.Random
    holdings: dict[tuple[str, str], Holding] = field(default_factory=dict)
    orders: dict[str, PaperOrder] = field(default_factory=dict)
    tags_seen: set[str] = field(default_factory=set)
    orders_received: int = 0
    duplicate_tags: int = 0
    place_calls: int = 0


class PaperBroker(BrokerAdapter):
    meta = BrokerMeta(
        name="paper", display_name="Paper (simulated)", required_credentials=(),
        optional_credentials=("seed_holdings", "reject_rate", "latency_ms", "rate_limit_every_n",
                              "ambiguous_rate", "ambiguous_placed", "partial_fill_rate", "fill_after_polls", "seed"),
        limits=RateLimits(10, 400, 10), live_tested=True, holdings_show_same_day_fills=True,
        credential_help="No credentials. Optional knobs override the PAPER_* env defaults for this session, "
                        "e.g. seed_holdings=INFY:10,TCS:5.")
    STATUS_MAP = {"pending": OrderStatus.PENDING, "open": OrderStatus.OPEN, "filled": OrderStatus.FILLED,
                  "rejected": OrderStatus.REJECTED, "cancelled": OrderStatus.CANCELLED}

    def __init__(self, http: Any, instruments: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(http, instruments, config)
        self.accounts: dict[str, PaperAccount] = {}

    async def complete_login(self, credentials: dict[str, str]) -> BrokerSession:
        faults = parse_faults(self.config, credentials)
        account = PaperAccount(faults=faults, rng=random.Random(faults.seed))
        seed = credentials.get("seed_holdings") or self.config.get("seed_holdings") or ""
        for part in filter(None, (p.strip() for p in seed.split(","))):
            symbol, _, qty = part.partition(":")
            inst = self.instruments.get(symbol.strip())
            if not qty.strip().isdigit():
                raise SymbolNotFoundError(f"seed_holdings entry {part!r} needs SYMBOL:QUANTITY")
            account.holdings[inst.key] = Holding(symbol=inst.symbol, exchange=inst.exchange, quantity=int(qty),
                                                 isin=inst.isin, average_price=price(inst.symbol))
        self.accounts[session_id := token_urlsafe(24)] = account
        return BrokerSession(session_id=session_id, broker="paper", user_id="paper",
                             access_token=SecretStr("paper"), expires_at=utcnow() + timedelta(hours=8))

    async def _enter(self, session: BrokerSession) -> PaperAccount:
        if session.session_id not in self.accounts:
            raise AuthError("unknown paper session")
        account = self.accounts[session.session_id]
        lo, hi = account.faults.latency_ms
        await asyncio.sleep(account.rng.uniform(lo, hi) / 1000)
        return account

    async def get_holdings(self, session: BrokerSession) -> list[Holding]:
        account = await self._enter(session)
        return sorted((h.model_copy() for h in account.holdings.values() if h.quantity > 0), key=lambda h: h.symbol)

    async def get_order(self, session: BrokerSession, broker_order_id: str) -> OrderUpdate:
        account = await self._enter(session)
        if broker_order_id not in account.orders:
            raise BrokerUnavailableError(f"unknown order {broker_order_id}")
        self._advance(account, account.orders[broker_order_id])
        return _update(account.orders[broker_order_id])

    async def list_orders(self, session: BrokerSession) -> list[OrderUpdate]:
        account = await self._enter(session)
        for order in account.orders.values():
            self._advance(account, order)
        return [_update(o) for o in account.orders.values()]

    async def place_order(self, session: BrokerSession, order: OrderRequest) -> str:
        account = await self._enter(session)
        account.place_calls += 1
        if account.faults.rate_limit_every_n and account.place_calls % account.faults.rate_limit_every_n == 0:
            raise RateLimitError("simulated 429", retry_after=0.05)
        account.orders_received += 1
        if order.tag in account.tags_seen:
            account.duplicate_tags += 1
        account.tags_seen.add(order.tag)
        if account.rng.random() < account.faults.ambiguous_rate:
            if account.faults.ambiguous_placed:
                self._record(account, order)
            raise AmbiguousOutcomeError("simulated read timeout after send")
        if order.symbol == "REJECTME":
            return self._record(account, order, reject_on_poll=True)
        if account.rng.random() < account.faults.reject_rate:
            raise OrderRejectedError("simulated rejection: insufficient funds")
        held = account.holdings.get((order.symbol, order.exchange))
        if order.side is OrderSide.SELL and order.quantity > (held.quantity if held else 0):
            raise OrderRejectedError("Holdings not available for sell")
        return self._record(account, order, partial=account.rng.random() < account.faults.partial_fill_rate)

    def _record(self, account: PaperAccount, order: OrderRequest, **flags: Any) -> str:
        broker_id = f"P{len(account.orders) + 1:06d}"
        account.orders[broker_id] = PaperOrder(broker_id, order.tag, order.symbol, order.exchange, order.side,
                                               order.quantity, **flags)
        return broker_id

    def _advance(self, account: PaperAccount, order: PaperOrder) -> None:
        order.polls_seen += 1
        if order.status != OrderStatus.OPEN or order.filled_qty or order.polls_seen < account.faults.fill_after_polls:
            return  # partials never progress
        if order.reject_on_poll:
            order.status = OrderStatus.REJECTED
        elif order.partial and order.quantity > 1:
            self._fill(account, order, order.quantity // 2)
        else:
            self._fill(account, order, order.quantity - order.filled_qty)
            order.status = OrderStatus.FILLED

    def _fill(self, account: PaperAccount, order: PaperOrder, qty: int) -> None:
        order.filled_qty += qty
        key = (order.symbol, order.exchange)
        held = account.holdings.get(key)
        delta = -qty if order.side is OrderSide.SELL else qty
        if held is None:
            isin = self.instruments.get(order.symbol, order.exchange).isin if key in self.instruments else None
            account.holdings[key] = Holding(symbol=order.symbol, exchange=order.exchange, quantity=max(0, delta),
                                            isin=isin, average_price=price(order.symbol))
        elif held.quantity + delta <= 0:
            del account.holdings[key]
        else:
            held.quantity += delta

