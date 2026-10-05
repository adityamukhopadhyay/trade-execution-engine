"""Zerodha Kite Connect v3. Written against the public docs; not run against a live account.
https://kite.trade/docs/connect/v3/
"""
from __future__ import annotations

import hashlib
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
    RateLimits,
)
from app.brokers.http import dec, json_or_text, next_ist, send
from app.core.models import BrokerSession, Holding, OrderRequest, OrderStatus, OrderUpdate

S = OrderStatus
REJECT_TYPES = {"InputException", "OrderException", "MarginException", "HoldingException", "UserException"}


class ZerodhaAdapter(BrokerAdapter):
    meta = BrokerMeta(
        name="zerodha", display_name="Zerodha Kite Connect",
        required_credentials=("api_key", "api_secret", "request_token"),
        limits=RateLimits(10, 400, 10), login_via_redirect=True, tag_max_len=20,
        credential_help="Create an app at developers.kite.trade; open the login URL, then paste the request_token "
                        "from the redirect (valid for a few minutes).")
    BASE_URL = "https://api.kite.trade"
    STATUS_MAP = {"complete": S.FILLED, "open": S.OPEN, "trigger pending": S.OPEN, "cancelled": S.CANCELLED,
                  "rejected": S.REJECTED, "put order req received": S.PENDING, "validation pending": S.PENDING,
                  "open pending": S.PENDING, "modify validation pending": S.PENDING, "modify pending": S.PENDING,
                  "cancel pending": S.PENDING, "amo req received": S.PENDING}

    def _headers(self, session: BrokerSession | None) -> dict[str, str]:
        headers = {"X-Kite-Version": "3"}
        if session is not None:
            api_key = session.api_key.get_secret_value() if session.api_key else ""
            headers["Authorization"] = f"token {api_key}:{session.access_token.get_secret_value()}"
        return headers

    async def _call(self, method: str, path: str, session: BrokerSession | None = None, *,
                    data: dict[str, Any] | None = None, is_order: bool = False, login: bool = False) -> Any:
        # Kite takes form-encoded bodies, not JSON
        request = self.http.build_request(method, self.base_url + path, data=data, headers=self._headers(session))
        body = json_or_text(await send(self.http, request, is_order=is_order))
        if isinstance(body, dict) and body.get("status") == "success":
            return body.get("data")
        self._raise(body, is_order=is_order, login=login)

    @staticmethod
    def _raise(body: Any, *, is_order: bool, login: bool) -> None:
        message = body.get("message", "unexpected response") if isinstance(body, dict) else str(body)[:200]
        error_type = body.get("error_type", "") if isinstance(body, dict) else ""
        if login or error_type == "TokenException":
            raise AuthError(message, raw=body)
        if is_order and error_type in REJECT_TYPES:
            raise OrderRejectedError(message, raw=body)
        if is_order:  # order may exist at broker
            raise AmbiguousOutcomeError(message, raw=body)
        raise BrokerUnavailableError(message, raw=body)

    @staticmethod
    def _to_broker_symbol(order: OrderRequest) -> dict[str, str]:
        return {"tradingsymbol": order.symbol, "exchange": order.exchange}

    def _update(self, row: dict[str, Any]) -> OrderUpdate:
        return OrderUpdate(broker_order_id=str(row.get("order_id")), status=self.map_status(row.get("status")),
                           filled_qty=int(row.get("filled_quantity") or 0), average_price=dec(row.get("average_price")),
                           message=row.get("status_message"), tag=row.get("tag"), raw_status=str(row.get("status")))

    def login_url(self, credentials: dict[str, str]) -> str | None:
        return f"https://kite.zerodha.com/connect/login?v=3&api_key={credentials.get('api_key', '')}"

    async def complete_login(self, credentials: dict[str, str]) -> BrokerSession:
        if missing := self.meta.missing_credentials(credentials):
            raise AuthError(f"missing credentials: {', '.join(missing)}")
        api_key, request_token = credentials["api_key"], credentials["request_token"]
        # checksum = sha256(api_key + request_token + api_secret), per the Kite docs
        checksum = hashlib.sha256(f"{api_key}{request_token}{credentials['api_secret']}".encode()).hexdigest()
        form = {"api_key": api_key, "request_token": request_token, "checksum": checksum}
        data = await self._call("POST", "/session/token", data=form, login=True)
        return BrokerSession(session_id=token_urlsafe(24), broker=self.meta.name, user_id=data.get("user_id"),
                             access_token=SecretStr(data["access_token"]), api_key=SecretStr(api_key),
                             expires_at=next_ist(6, 0))

    async def get_holdings(self, session: BrokerSession) -> list[Holding]:
        rows = await self._call("GET", "/portfolio/holdings", session) or []
        return [Holding(symbol=r["tradingsymbol"], exchange=r.get("exchange", "NSE"), isin=r.get("isin"),
                        quantity=int(r.get("quantity") or 0) - int(r.get("used_quantity") or 0),
                        average_price=dec(r.get("average_price")), last_price=dec(r.get("last_price")))
                for r in rows if r.get("exchange", "NSE") in ("NSE", "BSE")]

    async def get_order(self, session: BrokerSession, broker_order_id: str) -> OrderUpdate:
        history = await self._call("GET", f"/orders/{broker_order_id}", session) or []
        if not history:
            raise BrokerUnavailableError(f"order {broker_order_id} not found")
        return self._update(history[-1])  # history endpoint; last row is current

    async def list_orders(self, session: BrokerSession) -> list[OrderUpdate]:
        return [self._update(r) for r in await self._call("GET", "/orders", session) or []]

    async def place_order(self, session: BrokerSession, order: OrderRequest) -> str:
        form = {**self._to_broker_symbol(order), "transaction_type": order.side.value, "order_type": "MARKET",
                "quantity": order.quantity, "product": "CNC", "validity": "DAY",
                "tag": order.tag[:self.meta.tag_max_len]}
        data = await self._call("POST", "/orders/regular", session, data=form, is_order=True)
        return str(data["order_id"])
