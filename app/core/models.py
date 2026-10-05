"""Domain models shared by every layer; no I/O and nothing broker-specific."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PositiveInt, SecretStr, field_validator, model_validator

Exchange = Literal["NSE", "BSE"]
Mode = Literal["first_time", "rebalance"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Phase(StrEnum):
    SELL = "SELL"  # first: sale proceeds fund the buys
    BUY = "BUY"    # after every SELL is terminal


class InstructionAction(StrEnum):
    SELL = "SELL"
    BUY = "BUY"
    REBALANCE = "REBALANCE"  # signed quantity_delta


class OrderStatus(StrEnum):
    """Lifecycle of one planned order. Members of TERMINAL_ORDER_STATUSES never change again."""

    PENDING = "PENDING"                    # planned, not yet sent
    OPEN = "OPEN"                          # accepted, still working
    AMBIGUOUS = "AMBIGUOUS"                # sent, no answer; locating by tag
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # poll budget hit, left open
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"                # may carry fills
    FAILED = "FAILED"                      # never placed
    TIMED_OUT = "TIMED_OUT"                # poll budget hit, nothing filled
    UNKNOWN = "UNKNOWN"                    # not found by tag; never resent
    SKIPPED = "SKIPPED"                    # SELL-phase halt

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_ORDER_STATUSES


TERMINAL_ORDER_STATUSES: frozenset[OrderStatus] = frozenset({
    OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED,
    OrderStatus.FAILED, OrderStatus.TIMED_OUT, OrderStatus.UNKNOWN, OrderStatus.SKIPPED,
})


class RunStatus(StrEnum):
    PLANNED = "PLANNED"                    # dry run, nothing placed
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"                # every order FILLED
    PARTIALLY_FAILED = "PARTIALLY_FAILED"  # some filled, some not
    FAILED = "FAILED"                      # nothing filled


class ErrorCode(StrEnum):
    VALIDATION = "VALIDATION"
    SYMBOL_UNKNOWN = "SYMBOL_UNKNOWN"
    AUTH = "AUTH"
    RATE_LIMIT = "RATE_LIMIT"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    REJECTED = "REJECTED"
    AMBIGUOUS_UNRESOLVED = "AMBIGUOUS_UNRESOLVED"
    POLL_TIMEOUT = "POLL_TIMEOUT"
    SELL_PHASE_HALTED = "SELL_PHASE_HALTED"
    INTERNAL = "INTERNAL"


class SymbolRef(BaseModel):
    symbol: str = Field(min_length=1, max_length=32, description="Exchange trading symbol, e.g. INFY")
    exchange: Exchange = "NSE"
    isin: str | None = Field(default=None, pattern=r"^IN[A-Z0-9]{10}$")

    @field_validator("symbol")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return v.strip().upper()

    @property
    def key(self) -> tuple[str, str]:
        return (self.symbol, self.exchange)


class PortfolioLine(SymbolRef):
    quantity: PositiveInt


class RebalanceInstruction(SymbolRef):
    action: InstructionAction
    quantity: PositiveInt | None = Field(default=None, description="SELL / BUY only")
    quantity_delta: int | None = Field(default=None, description="REBALANCE only; +buy / -sell, never 0")

    @model_validator(mode="after")
    def _shape(self) -> RebalanceInstruction:
        if self.action is InstructionAction.REBALANCE:
            if self.quantity is not None or not self.quantity_delta:
                raise ValueError("REBALANCE takes a non-zero quantity_delta and no quantity")
        elif self.quantity is None or self.quantity_delta is not None:
            raise ValueError(f"{self.action} takes quantity and no quantity_delta")
        return self


class FirstTimePayload(BaseModel):
    mode: Literal["first_time"]
    lines: list[PortfolioLine] = Field(min_length=1, max_length=200)


class RebalancePayload(BaseModel):
    mode: Literal["rebalance"]
    instructions: list[RebalanceInstruction] = Field(min_length=1, max_length=200)


Portfolio = Annotated[FirstTimePayload | RebalancePayload, Field(discriminator="mode")]


class ExecuteRequest(BaseModel):
    session_id: str
    portfolio: Portfolio
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)
    dry_run: bool = False
    allow_existing_holdings: bool = False
    on_sell_shortfall: Literal["halt", "continue"] = "halt"

    def payload_hash(self) -> str:
        """Hash of everything except the idempotency key."""
        return hashlib.sha256(self.model_dump_json(exclude={"idempotency_key"}).encode()).hexdigest()


class Holding(BaseModel):
    symbol: str
    exchange: Exchange = "NSE"
    quantity: int = Field(ge=0, description="sellable (free) quantity")
    isin: str | None = None
    average_price: Decimal | None = None
    last_price: Decimal | None = None


class OrderRequest(BaseModel):
    """What the engine hands an adapter. Always MARKET, delivery product, DAY validity."""

    tag: str = Field(pattern=r"^[A-Za-z0-9]{8,20}$", description="client order tag kp{run8}{seq:03d}")
    symbol: str
    exchange: Exchange
    isin: str | None
    side: OrderSide
    quantity: PositiveInt


class PlannedOrder(OrderRequest):
    seq: int
    phase: Phase
    source_action: InstructionAction


class OrderUpdate(BaseModel):
    """Adapter -> engine: one broker order as the broker currently reports it."""

    broker_order_id: str
    status: OrderStatus                 # only PENDING, OPEN, FILLED, REJECTED, CANCELLED
    filled_qty: int = 0
    average_price: Decimal | None = None
    message: str | None = None
    tag: str | None = None
    raw_status: str | None = None


class OrderResult(PlannedOrder):
    status: OrderStatus = OrderStatus.PENDING
    broker_order_id: str | None = None
    filled_qty: int = 0
    average_price: Decimal | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    broker_status_raw: str | None = None
    attempts: int = 0
    submitted_at: datetime | None = None
    finalized_at: datetime | None = None

    @classmethod
    def from_planned(cls, order: PlannedOrder) -> OrderResult:
        return cls(**order.model_dump())


class ExecutionPlan(BaseModel):
    run_id: str
    session_id: str = Field(exclude=True)  # a bearer capability: used for listing, never serialised
    broker: str
    mode: Mode
    sells: list[PlannedOrder]
    buys: list[PlannedOrder]
    warnings: list[str] = Field(default_factory=list)
    holdings_before: list[Holding]
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def orders(self) -> list[PlannedOrder]:
        return [*self.sells, *self.buys]


class RunSummary(BaseModel):
    total: int = 0
    filled: int = 0
    partially_filled: int = 0
    rejected: int = 0
    cancelled: int = 0
    failed: int = 0
    timed_out: int = 0
    unknown: int = 0
    skipped: int = 0


class SymbolDiff(BaseModel):
    symbol: str
    exchange: Exchange
    expected: int
    actual: int
    explanation: str | None = None  # explained diffs are not mismatches


class ReconciliationResult(BaseModel):
    status: Literal["MATCH", "MISMATCH", "UNAVAILABLE"]
    diffs: list[SymbolDiff] = Field(default_factory=list)
    note: str | None = None


class ExecutionReport(BaseModel):
    """The one envelope: stored by the run store, returned by the API, carried by every notification."""

    run_id: str
    idempotency_key: str | None
    broker: str
    mode: Mode
    status: RunStatus
    dry_run: bool
    on_sell_shortfall: Literal["halt", "continue"]
    plan: ExecutionPlan
    orders: list[OrderResult]
    summary: RunSummary
    reconciliation: ReconciliationResult | None = None
    started_at: datetime
    finished_at: datetime | None = None


EventType = Literal["run.snapshot", "run.started", "run.phase", "order.updated", "run.completed"]


class ExecutionEvent(BaseModel):
    """Notification frame. Identical JSON on the console, in the webhook body and on the WebSocket."""

    type: EventType
    ts: datetime = Field(default_factory=utcnow)
    run_id: str
    phase: Phase | None = None
    order: OrderResult | None = None  # set for order.updated
    report: ExecutionReport


class SessionInfo(BaseModel):
    """The only session shape the API ever returns."""

    session_id: str
    broker: str
    user_id: str | None
    expires_at: datetime


class BrokerSession(BaseModel):
    session_id: str
    broker: str
    user_id: str | None = None
    access_token: SecretStr
    api_key: SecretStr | None = None
    extra: dict[str, SecretStr] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return utcnow() >= self.expires_at

    def public(self) -> SessionInfo:
        return SessionInfo(session_id=self.session_id, broker=self.broker,
                           user_id=self.user_id, expires_at=self.expires_at)


class BrokerInfo(BaseModel):
    name: str
    display_name: str
    required_credentials: list[str]
    optional_credentials: list[str]
    credential_help: str
    login_via_redirect: bool
    supports_client_tag: bool
    live_tested: bool
