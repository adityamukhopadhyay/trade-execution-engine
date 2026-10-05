"""Fyers API v3. Written against the public docs; not run against a live account.
https://myapi.fyers.in/docsv3
"""
from __future__ import annotations

import hashlib
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
    RateLimits,
)
from app.brokers.http import dec, json_or_text, next_ist, send
from app.core.models import BrokerSession, Holding, OrderRequest, OrderStatus, OrderUpdate

S = OrderStatus
AUTH_CODES = {-8, -15, -16, -17}
SERIES = {"NSE": "EQ", "BSE": "A"}  # BSE series letter unverified


class FyersAdapter(BrokerAdapter):
    meta = BrokerMeta(
        name="fyers", display_name="Fyers API v3",
        required_credentials=("app_id", "app_secret", "redirect_uri", "auth_code"),
        limits=RateLimits(10, 100, 10, 100), login_via_redirect=True, tag_max_len=30,
        credential_help="app_id looks like XC4XXXXXM-100; open the login URL, then paste auth_code from the redirect.")
    BASE_URL = "https://api-t1.fyers.in/api/v3"
    STATUS_MAP = {"2": S.FILLED, "6": S.OPEN, "4": S.PENDING, "5": S.REJECTED, "1": S.CANCELLED, "7": S.CANCELLED}

    @staticmethod
    def _headers(session: BrokerSession | None) -> dict[str, str]:
        if session is None:
            return {}
        app_id = session.api_key.get_secret_value() if session.api_key else ""
        return {"Authorization": f"{app_id}:{session.access_token.get_secret_value()}"}

    async def _call(self, method: str, path: str, session: BrokerSession | None = None, *,
                    json: dict[str, Any] | None = None, is_order: bool = False, login: bool = False) -> dict[str, Any]:
        request = self.http.build_request(method, self.base_url + path, json=json, headers=self._headers(session))
        body = json_or_text(await send(self.http, request, is_order=is_order))
        if isinstance(body, dict) and body.get("s") == "ok":
            return body
        self._raise(body, is_order=is_order, login=login)

    @staticmethod
    def _raise(body: Any, *, is_order: bool, login: bool) -> None:
        message = body.get("message", "unexpected response") if isinstance(body, dict) else str(body)[:200]
        code = body.get("code") if isinstance(body, dict) else None
        if login or code in AUTH_CODES:
            raise AuthError(message, raw=body)
        if is_order and isinstance(body, dict):
            raise OrderRejectedError(message, raw=body)
        if is_order:
            raise AmbiguousOutcomeError(message, raw=body)
        raise BrokerUnavailableError(message, raw=body)

    @staticmethod
    def _to_broker_symbol(order: OrderRequest) -> str:
        return f"{order.exchange}:{order.symbol}-{SERIES[order.exchange]}"

    @staticmethod
    def _from_broker_symbol(symbol: str) -> tuple[str, str]:
        """'NSE:SBIN-EQ' -> ('SBIN', 'NSE'); the series suffix is dropped."""
        exchange, _, rest = symbol.partition(":")
        return rest.rsplit("-", 1)[0], exchange or "NSE"

    def _update(self, row: dict[str, Any]) -> OrderUpdate:
        return OrderUpdate(broker_order_id=str(row.get("id")), status=self.map_status(row.get("status")),
                           filled_qty=int(row.get("filledQty") or 0), average_price=dec(row.get("tradedPrice")),
                           message=row.get("message") or None, tag=row.get("orderTag"),
                           raw_status=str(row.get("status")))

    def login_url(self, credentials: dict[str, str]) -> str | None:
        query = urlencode({"client_id": credentials.get("app_id", ""), "response_type": "code", "state": "kalpi",
                           "redirect_uri": credentials.get("redirect_uri", "")})
        return f"{self.base_url}/generate-authcode?{query}"

    async def complete_login(self, credentials: dict[str, str]) -> BrokerSession:
        if missing := self.meta.missing_credentials(credentials):
            raise AuthError(f"missing credentials: {', '.join(missing)}")
        app_id = credentials["app_id"]
        app_id_hash = hashlib.sha256(f"{app_id}:{credentials['app_secret']}".encode()).hexdigest()
        body = {"grant_type": "authorization_code", "appIdHash": app_id_hash, "code": credentials["auth_code"]}
        data = await self._call("POST", "/validate-authcode", json=body, login=True)
        return BrokerSession(session_id=token_urlsafe(24), broker=self.meta.name, user_id=None,
                             access_token=SecretStr(data["access_token"]), api_key=SecretStr(app_id),
                             expires_at=next_ist(6, 0))  # expiry hour unverified

    async def get_holdings(self, session: BrokerSession) -> list[Holding]:
        data = await self._call("GET", "/holdings", session)
        holdings = []
        for r in data.get("holdings") or []:
            symbol, exchange = self._from_broker_symbol(str(r.get("symbol", "")))
            if exchange in ("NSE", "BSE"):
                holdings.append(Holding(symbol=symbol, exchange=exchange, quantity=int(r.get("quantity") or 0),
                                        isin=r.get("isin"), average_price=dec(r.get("costPrice")),
                                        last_price=dec(r.get("ltp"))))
        return holdings

    @staticmethod
    def _book(data: dict[str, Any]) -> list[dict[str, Any]]:
        return data.get("orderBook") or data.get("data") or []

    async def get_order(self, session: BrokerSession, broker_order_id: str) -> OrderUpdate:
        rows = self._book(await self._call("GET", f"/orders?id={broker_order_id}", session))
        if not rows:
            raise BrokerUnavailableError(f"order {broker_order_id} not found")
        return self._update(rows[0])

    async def list_orders(self, session: BrokerSession) -> list[OrderUpdate]:
        return [self._update(r) for r in self._book(await self._call("GET", "/orders", session))]

    async def place_order(self, session: BrokerSession, order: OrderRequest) -> str:
        body = {"symbol": self._to_broker_symbol(order), "qty": order.quantity, "type": 2,
                "side": 1 if order.side.value == "BUY" else -1, "productType": "CNC", "limitPrice": 0,
                "stopPrice": 0, "validity": "DAY", "disclosedQty": 0, "offlineOrder": False,
                "orderTag": order.tag[:self.meta.tag_max_len]}
        data = await self._call("POST", "/orders/sync", session, json=body, is_order=True)
        return str(data["id"])
