"""The broker adapter contract: every broker, paper or real, implements exactly this."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from app.brokers.ratelimit import BrokerLimiter, RateLimits
from app.core.errors import SymbolNotFoundError  # noqa: F401  re-export
from app.core.instruments import InstrumentTable
from app.core.models import BrokerInfo, BrokerSession, Holding, OrderRequest, OrderStatus, OrderUpdate


class BrokerError(Exception):
    """Base of everything an adapter raises. `message` is safe to show a user; `raw` is for logs only."""

    def __init__(self, message: str, *, raw: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.raw = raw


class AuthError(BrokerError):
    """401/403, expired or invalid token, bad credentials. Never retried; the run fails."""


class RateLimitError(BrokerError):
    """429 (Angel One: a 403 with a non-JSON body). Refused before placement, so safe to retry after
    backoff. `retry_after` is seconds when the broker says."""

    def __init__(self, message: str, *, retry_after: float | None = None, raw: Any = None) -> None:
        super().__init__(message, raw=raw)
        self.retry_after = retry_after


class BrokerUnavailableError(BrokerError):
    """Connect error / connect timeout (the request never left), or any 5xx / transport error on a READ.
    Provably nothing was placed: retryable."""


class OrderRejectedError(BrokerError):
    """Synchronous business rejection (validation / RMS / margin / holdings). Deterministic: never retried."""

    def __init__(self, reason: str, *, raw: Any = None) -> None:
        super().__init__(reason, raw=raw)
        self.reason = reason


class AmbiguousOutcomeError(BrokerError):
    """An order was sent and no definitive answer came back (read timeout, 5xx, reset). The order may
    exist at the broker, so the engine never resends; it locates the order by tag."""


@dataclass(frozen=True, slots=True)
class BrokerMeta:
    name: str
    display_name: str
    required_credentials: tuple[str, ...]
    limits: RateLimits
    optional_credentials: tuple[str, ...] = ()
    credential_help: str = ""
    login_via_redirect: bool = False
    supports_client_tag: bool = True            # False: find_order_by_tag() returns None
    tag_max_len: int = 20
    live_tested: bool = False
    holdings_show_same_day_fills: bool = False  # real brokers settle T+1

    def missing_credentials(self, credentials: dict[str, str]) -> list[str]:
        return [f for f in self.required_credentials if not credentials.get(f)]

    def info(self) -> BrokerInfo:
        return BrokerInfo(
            name=self.name, display_name=self.display_name,
            required_credentials=list(self.required_credentials),
            optional_credentials=list(self.optional_credentials),
            credential_help=self.credential_help, login_via_redirect=self.login_via_redirect,
            supports_client_tag=self.supports_client_tag, live_tested=self.live_tested,
        )


class BrokerAdapter(ABC):
    meta: ClassVar[BrokerMeta]
    STATUS_MAP: ClassVar[dict[str, OrderStatus]] = {}  # lower-cased raw status -> canonical
    BASE_URL: ClassVar[str] = ""

    def __init__(self, http: httpx.AsyncClient, instruments: InstrumentTable,
                 config: dict[str, Any] | None = None) -> None:
        self.http = http
        self.instruments = instruments
        self.config = config or {}
        self.base_url: str = self.config.get("base_url", self.BASE_URL)
        self.limiter = BrokerLimiter(self.meta.limits, max_inflight=int(self.config.get("max_inflight", 4)))

    def login_url(self, credentials: dict[str, str]) -> str | None:
        """Login URL for redirect-style brokers, built from the non-secret credential fields only.
        None for direct-credential brokers. No I/O."""
        return None

    @abstractmethod
    async def complete_login(self, credentials: dict[str, str]) -> BrokerSession:
        """Exchange credentials (plus the callback token for redirect brokers) for a verified BrokerSession.
        Never store or log `credentials`. Raises AuthError, BrokerUnavailableError, RateLimitError."""

    @abstractmethod
    async def get_holdings(self, session: BrokerSession) -> list[Holding]:
        """Demat holdings in canonical symbols; quantity = sellable. Empty list for a fresh account."""

    @abstractmethod
    async def get_order(self, session: BrokerSession, broker_order_id: str) -> OrderUpdate:
        """Current state of one order, status mapped through STATUS_MAP."""

    @abstractmethod
    async def list_orders(self, session: BrokerSession) -> list[OrderUpdate]:
        """Today's order book with `tag` populated wherever the broker echoes it."""

    async def find_order_by_tag(self, session: BrokerSession, tag: str) -> OrderUpdate | None:
        """Locate an order whose outcome was ambiguous; None when the tag is absent from the book. The default
        scans list_orders() for an echoed tag ending in `tag` (Fyers may prefix it); override when the broker
        has a lookup-by-reference endpoint."""
        if not self.meta.supports_client_tag:
            return None
        return next((o for o in await self.list_orders(session) if o.tag and o.tag.endswith(tag)), None)

    @abstractmethod
    async def place_order(self, session: BrokerSession, order: OrderRequest) -> str:
        """Submit one MARKET delivery DAY order tagged with `order.tag`; return the broker order id.
        Raises OrderRejectedError, AuthError, RateLimitError, BrokerUnavailableError (provably not sent),
        AmbiguousOutcomeError (sent, unconfirmed) or SymbolNotFoundError."""

    def map_status(self, raw: str | int | None) -> OrderStatus:
        """Case-insensitive STATUS_MAP lookup. Unknown values count as OPEN so the poller keeps watching."""
        return self.STATUS_MAP.get(str(raw).strip().lower(), OrderStatus.OPEN)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.meta.name}>"
