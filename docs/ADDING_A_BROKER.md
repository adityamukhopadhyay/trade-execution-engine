# Adding a broker

`tests/test_registry.py` proves the mechanics with a toy `AcmeAdapter`: a sixth broker is one module under `app/brokers/` plus one entry in `ADAPTERS` in `app/brokers/registry.py`. This page does it for Dhan (DhanHQ v2): complete static docs, a sandbox that needs no trading account, and a numeric-id symbol model unlike the ticker-based brokers already here.

## What the contract asks for

`BrokerAdapter` in `app/brokers/base.py` has five abstract methods: `complete_login`, `get_holdings`, `get_order`, `list_orders`, `place_order`. `login_url` is optional (a URL for redirect brokers, `None` otherwise) and `find_order_by_tag` defaults to a scan of `list_orders`. Every request goes through `app.brokers.http.send`, which already turns connect errors, timeouts, 5xx, 429 and 401/403 into the right exception, so the adapter only parses the broker's own envelope.

## Dhan facts that shape the adapter

- Base URL `https://api.dhan.co/v2`; the sandbox is `https://sandbox.dhan.co` with the same paths. Auth is one header, `access-token`, a JWT generated on web.dhan.co (24-hour expiry, no refresh). Order placement needs the server IP whitelisted. `dhanClientId` goes in the order body, so keep it in `session.user_id`.
- Errors: `{"errorType", "errorCode": "DH-905", "errorMessage"}`; DH-901/902 auth, DH-904 rate limit, DH-905/906 input and order rejections. Order APIs allow 10/s and 250/min, non-trading calls 20/s.
- Equities are `exchangeSegment` (`NSE_EQ`, `BSE_EQ`) plus a numeric `securityId` from the scrip master CSV at `https://images.dhan.co/api-data/api-scrip-master.csv`. `correlationId` (up to 25 characters) is the client tag; `GET /orders/external/{correlationId}` finds an order by it.
- Statuses: `TRANSIT`, `PENDING`, `PART_TRADED`, `TRADED`, `REJECTED`, `CANCELLED`, `EXPIRED`. Placing answers a bare `{"orderId", "orderStatus"}`, no envelope. Fills are `filledQty` and `averageTradedPrice`, the rejection reason `omsErrorDescription`.

## The stub

`app/brokers/dhan.py`, with the real signatures from `base.py`; the API layer rejects missing credential fields, so `complete_login` only verifies the token.

```python
from __future__ import annotations

from datetime import timedelta
from secrets import token_urlsafe
from typing import Any

from pydantic import SecretStr

from app.brokers.base import (AuthError, BrokerAdapter, BrokerMeta, BrokerUnavailableError, OrderRejectedError,
                              RateLimitError, RateLimits, SymbolNotFoundError)
from app.brokers.http import dec, json_or_text, send
from app.core.models import BrokerSession, Holding, OrderRequest, OrderStatus, OrderUpdate, utcnow

S = OrderStatus
SEGMENT = {"NSE": "NSE_EQ", "BSE": "BSE_EQ"}
ERRORS = {"DH-901": AuthError, "DH-902": AuthError, "DH-904": RateLimitError}


class DhanAdapter(BrokerAdapter):
    meta = BrokerMeta(name="dhan", display_name="Dhan (DhanHQ v2)", limits=RateLimits(10, 250, 20),
                      required_credentials=("client_id", "access_token"), tag_max_len=25,
                      credential_help="24-hour token from web.dhan.co; orders need this server's IP whitelisted.")
    BASE_URL = "https://api.dhan.co/v2"
    STATUS_MAP = {"transit": S.PENDING, "pending": S.OPEN, "part_traded": S.OPEN, "traded": S.FILLED,
                  "rejected": S.REJECTED, "cancelled": S.CANCELLED, "expired": S.CANCELLED}

    async def _call(self, method: str, path: str, session: BrokerSession, *, json: Any = None,
                    is_order: bool = False) -> Any:
        headers = {"access-token": session.access_token.get_secret_value(), "Accept": "application/json"}
        request = self.http.build_request(method, self.base_url + path, json=json, headers=headers)
        body = json_or_text(await send(self.http, request, is_order=is_order))
        if not (code := body.get("errorCode") if isinstance(body, dict) else None):
            return body
        exc = ERRORS.get(code, OrderRejectedError if is_order else BrokerUnavailableError)
        raise exc(body.get("errorMessage") or "unexpected response", raw=body)

    def _update(self, row: dict[str, Any]) -> OrderUpdate:
        return OrderUpdate(broker_order_id=str(row.get("orderId")), status=self.map_status(row.get("orderStatus")),
                           filled_qty=int(row.get("filledQty") or 0), tag=row.get("correlationId") or None,
                           average_price=dec(row.get("averageTradedPrice")), message=row.get("omsErrorDescription"))

    async def complete_login(self, credentials: dict[str, str]) -> BrokerSession:
        session = BrokerSession(session_id=token_urlsafe(24), broker=self.meta.name, user_id=credentials["client_id"],
                                access_token=SecretStr(credentials["access_token"]),
                                expires_at=utcnow() + timedelta(hours=24))
        await self._call("GET", "/holdings", session)  # one cheap read proves the pasted token works
        return session

    async def get_holdings(self, session: BrokerSession) -> list[Holding]:
        return [Holding(symbol=r["tradingSymbol"], exchange="BSE" if r.get("exchange") == "BSE" else "NSE",
                        quantity=int(r.get("availableQty") or 0), isin=r.get("isin"),
                        average_price=dec(r.get("avgCostPrice")))
                for r in await self._call("GET", "/holdings", session) or []]

    async def get_order(self, session: BrokerSession, broker_order_id: str) -> OrderUpdate:
        row = await self._call("GET", f"/orders/{broker_order_id}", session)
        if not row:
            raise BrokerUnavailableError(f"order {broker_order_id} not found")
        return self._update(row[0] if isinstance(row, list) else row)

    async def list_orders(self, session: BrokerSession) -> list[OrderUpdate]:
        return [self._update(r) for r in await self._call("GET", "/orders", session) or []]

    async def place_order(self, session: BrokerSession, order: OrderRequest) -> str:
        security_id = self.instruments.get(order.symbol, order.exchange).dhan_security_id
        if not security_id:
            raise SymbolNotFoundError(f"{order.symbol}: no Dhan securityId in the instrument table")
        body = {"dhanClientId": session.user_id, "correlationId": order.tag[:self.meta.tag_max_len],
                "transactionType": order.side.value, "exchangeSegment": SEGMENT[order.exchange],
                "productType": "CNC", "orderType": "MARKET", "validity": "DAY", "securityId": security_id,
                "quantity": order.quantity, "price": 0, "disclosedQuantity": 0, "triggerPrice": 0,
                "afterMarketOrder": False}
        return str((await self._call("POST", "/orders", session, json=body, is_order=True))["orderId"])
```

Override `find_order_by_tag` with one `GET /orders/external/{tag}` call; the default book scan works but spends more reads.

## Register it

Import `DhanAdapter` in `app/brokers/registry.py` and add it to the tuple:

```python
ADAPTERS = (PaperBroker, ZerodhaAdapter, FyersAdapter, AngelOneAdapter, UpstoxAdapter, GrowwAdapter, DhanAdapter)
```

`GET /brokers` and the UI read everything else from `meta`. `tests/test_registry.py` pins the built-in set, so add `"dhan"` to `BUILT_IN` and pick another name for its unknown-broker test. For a `DHAN_BASE_URL` override, add `dhan_base_url` to `Settings` and to `base_urls` in `broker_configs()`.

## Symbol mapping

The one place a numeric-id broker costs more than one file: `data/instruments.json` carries `isin` and `angel_token` per symbol but no Dhan id. Do what Angel One did: add `dhan_security_id: str | None = None` to `Instrument` in `app/core/instruments.py` (the existing JSON still loads) and have `scripts/refresh_instruments.py` join the Dhan master on ISIN. A missing id raises `SymbolNotFoundError`, which becomes a 422 before anything is placed.

## Fixtures and contract tests

Create `tests/adapters/fixtures/dhan/` with one JSON body per case, from the docs or the sandbox: `holdings`, `login_fail` (DH-901), `place_ok`, `place_reject` (DH-905), `order_open`, `order_complete`, `order_rejected`, `order_book`, `order_by_correlation`, `rate_limited` (DH-904). `tests/adapters/test_dhan.py` then mirrors `test_zerodha.py` with the shared `router`, `http` and `instruments` fixtures from `tests/adapters/conftest.py`: login sends the header and verifies the token with a holdings read; `place_order` sends exactly the body above with the tag in `correlationId`; DH-905 is an `OrderRejectedError`; a 502 or read timeout on place is `AmbiguousOutcomeError`, a connect error `BrokerUnavailableError`; the reads map statuses; and every fixture status is in `STATUS_MAP`. A dozen tests, no network.

## Before calling it done

Run it against the sandbox (developer.dhanhq.co gives a client id and token without a Dhan account) with `base_url` at `https://sandbox.dhan.co/v2`, place a one-share BUY and confirm the correlation lookup finds it. Two open points for the fixtures to pin: whether `GET /orders/{id}` answers an object or a one-element list (the stub accepts both) and whether a rate-limit breach is an HTTP 429 or a 200 carrying DH-904. Leave `live_tested=False` until a real account has placed an order.
