"""Upstox API: v2 for login and reads, v3 for order placement (the v2 place endpoint is deprecated).
Written against the public docs; not run against a live account.
https://upstox.com/developer/api-documentation/
"""
from __future__ import annotations

import logging
from secrets import token_urlsafe
from typing import Any
from urllib.parse import urlencode

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
)
from app.brokers.http import dec, json_or_text, next_ist, send
from app.core.models import BrokerSession, Holding, OrderRequest, OrderStatus, OrderUpdate

log = logging.getLogger(__name__)
S = OrderStatus
AUTH_CODES = {"UDAPI100050", "UDAPI100016", "UDAPI100073"}
THROTTLE_CODES = {"UDAPI10005"}


class UpstoxAdapter(BrokerAdapter):
    meta = BrokerMeta(
        name="upstox", display_name="Upstox",
        required_credentials=("api_key", "api_secret", "redirect_uri", "code"),
        limits=RateLimits(10, 500, 50, 500), login_via_redirect=True, tag_max_len=40,
        credential_help="Open the login URL, then paste the `code` query parameter from the redirect (single use).")
    BASE_URL = "https://api.upstox.com/v2"
    ORDER_BASE_URL = "https://api-hft.upstox.com/v3"
    STATUS_MAP = {"complete": S.FILLED, "open": S.OPEN, "trigger pending": S.OPEN, "modified": S.OPEN,
                  "not modified": S.OPEN, "not cancelled": S.OPEN, "rejected": S.REJECTED, "cancelled": S.CANCELLED,
                  "cancelled after market order": S.CANCELLED, "validation pending": S.PENDING,
                  "put order req received": S.PENDING, "open pending": S.PENDING, "modify pending": S.PENDING,
                  "modify validation pending": S.PENDING, "cancel pending": S.PENDING,
                  "after market order req received": S.PENDING, "modify after market order req received": S.PENDING}

    def __init__(self, http: Any, instruments: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(http, instruments, config)
        self.order_base_url: str = self.config.get("order_base_url", self.ORDER_BASE_URL)

    @staticmethod
    def _headers(session: BrokerSession | None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if session is not None:
            headers["Authorization"] = f"Bearer {session.access_token.get_secret_value()}"
        return headers

    async def _call(self, method: str, url: str, session: BrokerSession | None = None, *,
                    json: dict[str, Any] | None = None, data: dict[str, Any] | None = None,
                    is_order: bool = False, login: bool = False) -> Any:
        request = self.http.build_request(method, url, json=json, data=data, headers=self._headers(session))
        body = json_or_text(await send(self.http, request, is_order=is_order))
        if isinstance(body, dict) and body.get("status") == "success":
            return body.get("data")
        if login and isinstance(body, dict) and body.get("access_token"):
            return body  # bare object, no envelope
        self._raise(body, is_order=is_order, login=login)

    @staticmethod
    def _raise(body: Any, *, is_order: bool, login: bool) -> None:
        errors = body.get("errors") or [{}] if isinstance(body, dict) else [{}]
        message = errors[0].get("message") or (body.get("message") if isinstance(body, dict) else str(body)[:200])
        code = errors[0].get("errorCode", "")
        if login or code in AUTH_CODES:
            raise AuthError(message or "login failed", raw=body)
        if code in THROTTLE_CODES:
            raise RateLimitError(message, raw=body)
        if is_order:
            raise OrderRejectedError(message or "order rejected", raw=body)
        raise BrokerUnavailableError(message or "unexpected response", raw=body)

    def _to_broker_symbol(self, order: OrderRequest) -> str:
        isin = order.isin or self.instruments.get(order.symbol, order.exchange).isin
        return f"{order.exchange}_EQ|{isin}"

    def _update(self, row: dict[str, Any]) -> OrderUpdate:
        return OrderUpdate(broker_order_id=str(row.get("order_id")), status=self.map_status(row.get("status")),
                           filled_qty=int(row.get("filled_quantity") or 0), average_price=dec(row.get("average_price")),
                           message=row.get("status_message") or None, tag=row.get("tag") or None,
                           raw_status=str(row.get("status")))

    def login_url(self, credentials: dict[str, str]) -> str | None:
        query = urlencode({"response_type": "code", "client_id": credentials.get("api_key", ""),
                           "redirect_uri": credentials.get("redirect_uri", "")})
        return f"{self.base_url}/login/authorization/dialog?{query}"

    async def complete_login(self, credentials: dict[str, str]) -> BrokerSession:
        if missing := self.meta.missing_credentials(credentials):
            raise AuthError(f"missing credentials: {', '.join(missing)}")
        form = {"code": credentials["code"], "client_id": credentials["api_key"],
                "client_secret": credentials["api_secret"], "redirect_uri": credentials["redirect_uri"],
                "grant_type": "authorization_code"}
        data = await self._call("POST", f"{self.base_url}/login/authorization/token", data=form, login=True)
        if data.get("poa") is False:
            log.warning("upstox: user %s has no POA/DDPI; CNC sells may need eDIS outside the API", data.get("user_id"))
        return BrokerSession(session_id=token_urlsafe(24), broker=self.meta.name, user_id=data.get("user_id"),
                             access_token=SecretStr(data["access_token"]), expires_at=next_ist(3, 30))

    async def get_holdings(self, session: BrokerSession) -> list[Holding]:
        rows = await self._call("GET", f"{self.base_url}/portfolio/long-term-holdings", session) or []
        return [Holding(symbol=r.get("trading_symbol") or r.get("tradingsymbol"), exchange=r.get("exchange", "NSE"),
                        quantity=int(r.get("quantity") or 0), isin=r.get("isin"),
                        average_price=dec(r.get("average_price")), last_price=dec(r.get("last_price")))
                for r in rows if r.get("exchange", "NSE") in ("NSE", "BSE")]

    async def get_order(self, session: BrokerSession, broker_order_id: str) -> OrderUpdate:
        row = await self._call("GET", f"{self.base_url}/order/details?order_id={broker_order_id}", session)
        if not row:
            raise BrokerUnavailableError(f"order {broker_order_id} not found")
        return self._update(row)

    async def list_orders(self, session: BrokerSession) -> list[OrderUpdate]:
        rows = await self._call("GET", f"{self.base_url}/order/retrieve-all", session) or []
        return [self._update(r) for r in rows]

    async def place_order(self, session: BrokerSession, order: OrderRequest) -> str:
        body = {"quantity": order.quantity, "product": "D", "validity": "DAY", "price": 0,
                "tag": order.tag[:self.meta.tag_max_len], "instrument_token": self._to_broker_symbol(order),
                "order_type": "MARKET", "transaction_type": order.side.value, "disclosed_quantity": 0,
                "trigger_price": 0, "is_amo": False}
        data = await self._call("POST", f"{self.order_base_url}/order/place", session, json=body, is_order=True) or {}
        order_id = data.get("order_id") or (data.get("order_ids") or [None])[0]
        if not order_id:
            raise AmbiguousOutcomeError("place accepted but no order id in the response", raw=data)
        return str(order_id)
