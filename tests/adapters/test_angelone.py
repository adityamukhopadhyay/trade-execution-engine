"""Contract tests for AngelOneAdapter against recorded fixtures."""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.brokers.angelone import UNIQUE_ID_CAP, AngelOneAdapter
from app.brokers.base import (
    AmbiguousOutcomeError,
    AuthError,
    BrokerUnavailableError,
    OrderRejectedError,
    RateLimitError,
    SymbolNotFoundError,
)
from app.core.models import OrderStatus
from tests.adapters.conftest import ADAPTER_STATUSES, load_fixture, make_order, make_session

BASE = "https://apiconnect.angelone.in"
ORDERS = f"{BASE}/rest/secure/angelbroking/order/v1"
CREDS = {"api_key": "angelkey", "client_code": "A12345", "mpin": "1234", "totp": "654321"}
pytestmark = pytest.mark.asyncio


def fx(name: str):
    return load_fixture("angelone", name)


def body_of(route) -> dict:
    return json.loads(route.calls.last.request.content)


@pytest.fixture
def adapter(http, instruments):
    return AngelOneAdapter(http, instruments)


@pytest.fixture
def session():
    return make_session("angelone", token="angel-jwt-token", api_key="angelkey")


async def test_no_login_url(adapter):
    assert adapter.login_url(CREDS) is None


async def test_login_posts_clientcode_mpin_totp(router, adapter):
    route = router.post(f"{BASE}/rest/auth/angelbroking/user/v1/loginByPassword").respond(200, json=fx("login_ok.json"))
    session = await adapter.complete_login(CREDS)
    request = route.calls.last.request
    assert body_of(route) == {"clientcode": "A12345", "password": "1234", "totp": "654321"}
    assert request.headers["X-PrivateKey"] == "angelkey" and request.headers["X-UserType"] == "USER"
    assert request.headers["X-SourceID"] == "WEB" and "Authorization" not in request.headers
    assert session.access_token.get_secret_value() == "angel-jwt-token" and session.user_id == "A12345"


async def test_login_status_false_is_auth_error(router, adapter):
    router.post(f"{BASE}/rest/auth/angelbroking/user/v1/loginByPassword").respond(200, json=fx("login_fail.json"))
    with pytest.raises(AuthError):
        await adapter.complete_login(CREDS)


async def test_holdings_strip_series_suffix(router, adapter, session):
    route = router.get(f"{BASE}/rest/secure/angelbroking/portfolio/v1/getAllHolding")
    route.respond(200, json=fx("holdings.json"))
    holdings = await adapter.get_holdings(session)
    headers = route.calls.last.request.headers
    assert headers["Authorization"] == "Bearer angel-jwt-token" and headers["X-PrivateKey"] == "angelkey"
    assert headers["X-MACAddress"] and headers["X-ClientLocalIP"] and headers["X-ClientPublicIP"]
    assert [(h.symbol, h.quantity, h.isin) for h in holdings] == [("TATASTEEL", 2, "INE081A01020"),
                                                                   ("INFY", 10, "INE009A01021")]
    assert holdings[0].average_price == Decimal("111.87") and holdings[0].last_price == Decimal("130.15")


async def test_null_data_means_no_holdings(router, adapter, session):
    router.get(f"{BASE}/rest/secure/angelbroking/portfolio/v1/getAllHolding").respond(
        200, json={"status": True, "message": "SUCCESS", "errorcode": "", "data": None})
    assert await adapter.get_holdings(session) == []


async def test_place_uses_symboltoken_and_string_numbers(router, adapter, session):
    route = router.post(f"{ORDERS}/placeOrder").respond(200, json=fx("place_ok.json"))
    assert await adapter.place_order(session, make_order()) == "200910000000111"
    assert body_of(route) == {"variety": "NORMAL", "tradingsymbol": "INFY-EQ", "symboltoken": "1594",
                              "transactiontype": "BUY", "exchange": "NSE", "ordertype": "MARKET",
                              "producttype": "DELIVERY", "duration": "DAY", "price": "0", "squareoff": "0",
                              "stoploss": "0", "quantity": "10", "ordertag": "kp1234abcd001"}
    assert adapter._unique_ids["200910000000111"] == "34reqfachdfih"


async def test_missing_angel_token_is_symbol_not_found(router, adapter, session):
    route = router.post(f"{ORDERS}/placeOrder").respond(200, json=fx("place_ok.json"))
    with pytest.raises(SymbolNotFoundError):
        await adapter.place_order(session, make_order(symbol="NOTOKEN", isin=None))
    with pytest.raises(SymbolNotFoundError):
        await adapter.place_order(session, make_order(symbol="UNKNOWN", isin=None))
    assert not route.called


async def test_place_errors(router, adapter, session):
    route = router.post(f"{ORDERS}/placeOrder")
    route.respond(200, json=fx("place_reject.json"))  # HTTP 200 with status:false
    with pytest.raises(OrderRejectedError) as info:
        await adapter.place_order(session, make_order())
    assert "Insufficient Funds" in info.value.reason
    route.respond(200, json={"status": False, "message": "Symbol Not Found", "errorcode": "AB1009", "data": None})
    with pytest.raises(SymbolNotFoundError):
        await adapter.place_order(session, make_order())
    route.respond(200, json={"status": False, "message": "Token Expired", "errorcode": "AG8002", "data": None})
    with pytest.raises(AuthError):
        await adapter.place_order(session, make_order())
    route.respond(502, text="Bad Gateway")
    with pytest.raises(AmbiguousOutcomeError):
        await adapter.place_order(session, make_order())
    route.mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(BrokerUnavailableError):
        await adapter.place_order(session, make_order())


async def test_403_plain_text_is_a_throttle_not_auth(router, adapter, session):
    route = router.get(f"{ORDERS}/getOrderBook")
    route.respond(403, text=fx("rate_limited.txt"))
    with pytest.raises(RateLimitError):
        await adapter.list_orders(session)
    route.respond(403, json={"status": False, "message": "Invalid Token", "errorcode": "AG8001", "data": None})
    with pytest.raises(AuthError):
        await adapter.list_orders(session)
    route.respond(500, text="oops")
    with pytest.raises(BrokerUnavailableError):
        await adapter.list_orders(session)


async def test_get_order_uses_uniqueorderid_then_falls_back_to_book(router, adapter, session):
    router.post(f"{ORDERS}/placeOrder").respond(200, json=fx("place_ok.json"))
    await adapter.place_order(session, make_order())
    details = router.get(f"{ORDERS}/details/34reqfachdfih")
    details.respond(200, json=fx("order_open.json"))
    assert (await adapter.get_order(session, "200910000000111")).status is OrderStatus.OPEN
    details.respond(200, json=fx("order_complete.json"))
    update = await adapter.get_order(session, "200910000000111")
    assert (update.status, update.filled_qty, update.average_price) == (OrderStatus.FILLED, 10, Decimal("1510.25"))
    details.respond(200, json=fx("order_rejected.json"))
    update = await adapter.get_order(session, "200910000000111")
    assert update.status is OrderStatus.REJECTED and update.message.startswith("RMS:")
    book = router.get(f"{ORDERS}/getOrderBook").respond(200, json=fx("order_book.json"))
    update = await adapter.get_order(session, "200910000000112")  # unknown id: scans the book
    assert update.status is OrderStatus.FILLED and book.called
    assert adapter._unique_ids["200910000000112"] == "35reqfachdfij"  # learned from the book row
    with pytest.raises(BrokerUnavailableError):
        await adapter.get_order(session, "nope")


async def test_unique_id_map_is_bounded(adapter):
    for i in range(UNIQUE_ID_CAP + 5):
        adapter._remember(str(i), f"u{i}")
    assert len(adapter._unique_ids) == UNIQUE_ID_CAP
    assert "4" not in adapter._unique_ids and adapter._unique_ids["5"] == "u5"


async def test_list_orders_and_find_by_tag(router, adapter, session):
    router.get(f"{ORDERS}/getOrderBook").respond(200, json=fx("order_book.json"))
    book = await adapter.list_orders(session)
    assert [o.tag for o in book] == ["kp1234abcd001", "kp1234abcd002", "kp1234abcd003", None]
    assert [o.status for o in book] == [OrderStatus.OPEN, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.OPEN]
    found = await adapter.find_order_by_tag(session, "kp1234abcd003")
    assert found is not None and found.broker_order_id == "200910000000113"


async def test_status_map_covers_every_fixture_status(adapter):
    raw = {fx(f"{n}.json")["data"]["orderstatus"] for n in ("order_open", "order_complete", "order_rejected")}
    raw |= {row["orderstatus"] for row in fx("order_book.json")["data"]}
    assert raw and all(status.lower() in adapter.STATUS_MAP for status in raw)
    assert set(adapter.STATUS_MAP.values()) <= ADAPTER_STATUSES
