"""Groww Trade API v1. Written against the public docs; not run against a live account.
https://groww.in/trade-api/docs/curl
"""
from __future__ import annotations

from secrets import token_urlsafe
from typing import Any

from pydantic import SecretStr

from app.brokers.base import (
    AuthError,
    BrokerAdapter,
    BrokerError,
    BrokerMeta,
    BrokerUnavailableError,
    OrderRejectedError,
    RateLimits,
    SymbolNotFoundError,
)
from app.brokers.http import dec, json_or_text, next_ist, send
from app.core.models import BrokerSession, Holding, OrderRequest, OrderStatus, OrderUpdate

S = OrderStatus
SYMBOL_CODES = {"GA001"}


class GrowwAdapter(BrokerAdapter):
    meta = BrokerMeta(
        name="groww", display_name="Groww Trade API",
        required_credentials=(), optional_credentials=("access_token", "api_key", "totp"),
        limits=RateLimits(10, 250, 20, 500), tag_max_len=20,
        credential_help="Paste today's access token from the Groww Trade API page, or give api_key + the current TOTP.")
    BASE_URL = "https://api.groww.in/v1"
    STATUS_MAP = {"executed": S.FILLED, "completed": S.FILLED, "delivery_awaited": S.FILLED, "open": S.OPEN,
                  "approved": S.OPEN, "trigger_pending": S.OPEN, "new": S.PENDING, "acked": S.PENDING,
                  "modification_requested": S.PENDING, "cancellation_requested": S.PENDING,
                  "rejected": S.REJECTED, "failed": S.REJECTED, "cancelled": S.CANCELLED}

    @staticmethod
    def _headers(session: BrokerSession | None, bearer: str = "") -> dict[str, str]:
        token = session.access_token.get_secret_value() if session is not None else bearer
        return {"Authorization": f"Bearer {token}", "X-API-VERSION": "1.0", "Accept": "application/json"}

    async def _call(self, method: str, path: str, session: BrokerSession | None = None, *,
                    json: dict[str, Any] | None = None, is_order: bool = False, login: bool = False,
                    bearer: str = "") -> Any:
        request = self.http.build_request(method, self.base_url + path, json=json,
                                          headers=self._headers(session, bearer))
        body = json_or_text(await send(self.http, request, is_order=is_order))
        if isinstance(body, dict) and body.get("status") == "SUCCESS":
            return body.get("payload")
        if login and isinstance(body, dict) and body.get("token"):
            return body  # bare object, no envelope
        self._raise(body, is_order=is_order, login=login)

    @staticmethod
    def _raise(body: Any, *, is_order: bool, login: bool) -> None:
        error = body.get("error") or {} if isinstance(body, dict) else {}
        message = error.get("message") or (body.get("message") if isinstance(body, dict) else str(body)[:200])
        if login:
            raise AuthError(message or "login failed", raw=body)
        if is_order and error.get("code") in SYMBOL_CODES:
            raise SymbolNotFoundError(message)
        if is_order:
            raise OrderRejectedError(message or "order rejected", raw=body)
        raise BrokerUnavailableError(message or "unexpected response", raw=body)

    @staticmethod
    def _to_broker_symbol(order: OrderRequest) -> dict[str, str]:
        return {"trading_symbol": order.symbol, "exchange": order.exchange, "segment": "CASH"}

    def _update(self, row: dict[str, Any]) -> OrderUpdate:
        raw = row.get("order_status")
        return OrderUpdate(broker_order_id=str(row.get("groww_order_id")), status=self.map_status(raw),
                           filled_qty=int(row.get("filled_quantity") or 0),
                           average_price=dec(row.get("average_fill_price")), message=row.get("remark") or None,
                           tag=row.get("order_reference_id") or None, raw_status=str(raw))

    async def complete_login(self, credentials: dict[str, str]) -> BrokerSession:
        token = credentials.get("access_token", "")
        # the token endpoint is capped at 150 calls a day, so a pasted token skips it
        if not token and credentials.get("api_key") and credentials.get("totp"):
            data = await self._call("POST", "/token/api/access", json={"key_type": "totp", "totp": credentials["totp"]},
                                    login=True, bearer=credentials["api_key"])
            token = data["token"]
        if not token:
            raise AuthError("give access_token, or api_key + totp")
        session = BrokerSession(session_id=token_urlsafe(24), broker=self.meta.name, user_id=None,
                                access_token=SecretStr(token), expires_at=next_ist(6, 0))
        await self._call("GET", "/holdings/user", session, login=True)  # verifies a pasted token
        return session

    async def get_holdings(self, session: BrokerSession) -> list[Holding]:
        payload = await self._call("GET", "/holdings/user", session) or {}
        return [Holding(symbol=r["trading_symbol"], exchange="NSE",  # no exchange in payload
                        quantity=int(r.get("demat_free_quantity") if r.get("demat_free_quantity") is not None
                                     else r.get("quantity") or 0),
                        isin=r.get("isin"), average_price=dec(r.get("average_price")), last_price=None)
                for r in payload.get("holdings") or []]

    async def get_order(self, session: BrokerSession, broker_order_id: str) -> OrderUpdate:
        row = await self._call("GET", f"/order/detail/{broker_order_id}?segment=CASH", session)
        if not row:
            raise BrokerUnavailableError(f"order {broker_order_id} not found")
        return self._update(row)

    async def list_orders(self, session: BrokerSession) -> list[OrderUpdate]:
        payload = await self._call("GET", "/order/list?segment=CASH&page=0&page_size=100", session) or {}
        return [self._update(r) for r in payload.get("order_list") or []]

    async def find_order_by_tag(self, session: BrokerSession, tag: str) -> OrderUpdate | None:
        """One call to the status-by-reference endpoint; any failure falls back to the default book scan."""
        try:
            row = await self._call("GET", f"/order/status/reference/{tag}?segment=CASH", session)
        except BrokerError:
            row = None
        if row and row.get("groww_order_id"):
            return self._update(row)
        return await super().find_order_by_tag(session, tag)

    async def place_order(self, session: BrokerSession, order: OrderRequest) -> str:
        body = {**self._to_broker_symbol(order), "quantity": order.quantity, "validity": "DAY", "product": "CNC",
                "order_type": "MARKET", "transaction_type": order.side.value,
                "order_reference_id": order.tag[:self.meta.tag_max_len]}
        payload = await self._call("POST", "/order/create", session, json=body, is_order=True) or {}
        return str(payload["groww_order_id"])
