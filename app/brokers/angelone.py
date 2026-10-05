"""Angel One SmartAPI. Written against the public docs; not run against a live account.
https://smartapi.angelbroking.com/docs
"""
from __future__ import annotations

from secrets import token_urlsafe
from typing import Any

from pydantic import SecretStr

from app.brokers.base import (
    AuthError,
    BrokerAdapter,
    BrokerMeta,
    BrokerUnavailableError,
    OrderRejectedError,
    RateLimits,
    SymbolNotFoundError,
)
from app.brokers.http import dec, json_or_text, next_ist, send
from app.core.models import BrokerSession, Holding, OrderRequest, OrderStatus, OrderUpdate

S = OrderStatus
AUTH_CODES = {"AG8001", "AG8002", "AG8003", "AB1010", "AB1011", "AB1000", "AB1006"}
SYMBOL_CODES = {"AB1009", "AB1018"}
SUFFIXES = ("-EQ", "-BE", "-SG", "-MF")
UNIQUE_ID_CAP = 10_000
CLIENT_HEADERS = {"X-UserType": "USER", "X-SourceID": "WEB", "X-ClientLocalIP": "127.0.0.1",
                  "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00:00:00:00:00:00",
                  "Content-Type": "application/json", "Accept": "application/json"}


class AngelOneAdapter(BrokerAdapter):
    meta = BrokerMeta(
        name="angelone", display_name="Angel One SmartAPI",
        required_credentials=("api_key", "client_code", "mpin", "totp"),
        limits=RateLimits(10, 500, 1), tag_max_len=19,
        credential_help="Enable TOTP at smartapi.angelbroking.com/enable-totp and enter the current 6-digit code.")
    BASE_URL = "https://apiconnect.angelone.in"
    STATUS_MAP = {"complete": S.FILLED, "open": S.OPEN, "trigger pending": S.OPEN, "modified": S.OPEN,
                  "rejected": S.REJECTED, "cancelled": S.CANCELLED, "cancelled after market order": S.CANCELLED,
                  "open pending": S.PENDING, "validation pending": S.PENDING, "modify pending": S.PENDING,
                  "after market order req received": S.PENDING, "modify after market order req received": S.PENDING,
                  "amo req received": S.PENDING}

    def __init__(self, http: Any, instruments: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(http, instruments, config)
        self._unique_ids: dict[str, str] = {}  # orderid -> uniqueorderid, newest UNIQUE_ID_CAP

    def _remember(self, order_id: str, unique_id: str) -> None:
        self._unique_ids[order_id] = unique_id
        while len(self._unique_ids) > UNIQUE_ID_CAP:
            del self._unique_ids[next(iter(self._unique_ids))]

    @staticmethod
    def _headers(session: BrokerSession | None, api_key: str = "") -> dict[str, str]:
        headers = {**CLIENT_HEADERS, "X-PrivateKey": api_key}
        if session is not None:
            headers["X-PrivateKey"] = session.api_key.get_secret_value() if session.api_key else ""
            headers["Authorization"] = f"Bearer {session.access_token.get_secret_value()}"
        return headers

    async def _call(self, method: str, path: str, session: BrokerSession | None = None, *, json: Any = None,
                    is_order: bool = False, login: bool = False, api_key: str = "") -> Any:
        request = self.http.build_request(method, self.base_url + path, json=json,
                                          headers=self._headers(session, api_key))
        body = json_or_text(await send(self.http, request, is_order=is_order))
        # failures often come back as HTTP 200 with status:false
        if isinstance(body, dict) and body.get("status") is True:
            return body.get("data")
        self._raise(body, is_order=is_order, login=login)

    @staticmethod
    def _raise(body: Any, *, is_order: bool, login: bool) -> None:
        message = body.get("message", "unexpected response") if isinstance(body, dict) else str(body)[:200]
        code = body.get("errorcode", "") if isinstance(body, dict) else ""
        if login or code in AUTH_CODES:
            raise AuthError(message, raw=body)
        if is_order and code in SYMBOL_CODES:
            raise SymbolNotFoundError(message)
        if is_order:
            raise OrderRejectedError(message, raw=body)
        raise BrokerUnavailableError(message, raw=body)

    def _to_broker_symbol(self, order: OrderRequest) -> tuple[str, str]:
        """(tradingsymbol, symboltoken); NSE symbols carry the -EQ series, BSE rows do not."""
        inst = self.instruments.get(order.symbol, order.exchange)
        if not inst.angel_token:
            raise SymbolNotFoundError(f"{order.symbol}: no Angel One symboltoken in the instrument table")
        return (f"{order.symbol}-EQ" if order.exchange == "NSE" else order.symbol), inst.angel_token

    @staticmethod
    def _from_broker_symbol(symbol: str) -> str:
        return next((symbol[:-len(s)] for s in SUFFIXES if symbol.endswith(s)), symbol)

    def _update(self, row: dict[str, Any]) -> OrderUpdate:
        if row.get("orderid") and row.get("uniqueorderid"):
            self._remember(str(row["orderid"]), str(row["uniqueorderid"]))
        raw = row.get("orderstatus") or row.get("status")
        return OrderUpdate(broker_order_id=str(row.get("orderid")), status=self.map_status(raw),
                           filled_qty=int(row.get("filledshares") or 0), average_price=dec(row.get("averageprice")),
                           message=row.get("text") or None, tag=row.get("ordertag") or None, raw_status=str(raw))

    async def complete_login(self, credentials: dict[str, str]) -> BrokerSession:
        if missing := self.meta.missing_credentials(credentials):
            raise AuthError(f"missing credentials: {', '.join(missing)}")
        body = {"clientcode": credentials["client_code"], "password": credentials["mpin"], "totp": credentials["totp"]}
        data = await self._call("POST", "/rest/auth/angelbroking/user/v1/loginByPassword", json=body, login=True,
                                api_key=credentials["api_key"]) or {}
        if not data.get("jwtToken"):
            raise AuthError("login answered without a jwtToken")
        return BrokerSession(session_id=token_urlsafe(24), broker=self.meta.name, user_id=credentials["client_code"],
                             access_token=SecretStr(data["jwtToken"]), api_key=SecretStr(credentials["api_key"]),
                             expires_at=next_ist(5, 0))

    async def get_holdings(self, session: BrokerSession) -> list[Holding]:
        data = await self._call("GET", "/rest/secure/angelbroking/portfolio/v1/getAllHolding", session) or {}
        return [Holding(symbol=self._from_broker_symbol(r.get("tradingsymbol", "")), exchange=r.get("exchange", "NSE"),
                        quantity=int(r.get("quantity") or 0), isin=r.get("isin"),
                        average_price=dec(r.get("averageprice")), last_price=dec(r.get("ltp")))
                for r in data.get("holdings") or [] if r.get("exchange", "NSE") in ("NSE", "BSE")]

    async def get_order(self, session: BrokerSession, broker_order_id: str) -> OrderUpdate:
        unique_id = self._unique_ids.get(broker_order_id)
        if unique_id:
            row = await self._call("GET", f"/rest/secure/angelbroking/order/v1/details/{unique_id}", session)
            if row:
                return self._update(row)
        found = next((o for o in await self.list_orders(session) if o.broker_order_id == broker_order_id), None)
        if found is None:
            raise BrokerUnavailableError(f"order {broker_order_id} not found")
        return found

    async def list_orders(self, session: BrokerSession) -> list[OrderUpdate]:
        rows = await self._call("GET", "/rest/secure/angelbroking/order/v1/getOrderBook", session) or []
        return [self._update(r) for r in rows]

    async def place_order(self, session: BrokerSession, order: OrderRequest) -> str:
        tradingsymbol, token = self._to_broker_symbol(order)
        body = {"variety": "NORMAL", "tradingsymbol": tradingsymbol, "symboltoken": token,
                "transactiontype": order.side.value, "exchange": order.exchange, "ordertype": "MARKET",
                "producttype": "DELIVERY", "duration": "DAY", "price": "0", "squareoff": "0", "stoploss": "0",
                "quantity": str(order.quantity), "ordertag": order.tag[:self.meta.tag_max_len]}
        data = await self._call("POST", "/rest/secure/angelbroking/order/v1/placeOrder", session, json=body,
                                is_order=True) or {}
        order_id = str(data["orderid"])
        if data.get("uniqueorderid"):
            self._remember(order_id, str(data["uniqueorderid"]))
        return order_id
