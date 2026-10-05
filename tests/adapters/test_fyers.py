"""Contract tests for FyersAdapter against recorded fixtures."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import httpx
import pytest

from app.brokers.base import (
    AmbiguousOutcomeError,
    AuthError,
    BrokerUnavailableError,
    OrderRejectedError,
    RateLimitError,
)
from app.brokers.fyers import FyersAdapter
from app.core.models import OrderStatus
from tests.adapters.conftest import ADAPTER_STATUSES, load_fixture, make_order, make_session

BASE = "https://api-t1.fyers.in/api/v3"
CREDS = {"app_id": "XC4XXXXXM-100", "app_secret": "secret", "redirect_uri": "https://app.example/cb",
         "auth_code": "authcode1"}
pytestmark = pytest.mark.asyncio


def fx(name: str):
    return load_fixture("fyers", name)


def body_of(route) -> dict:
    return json.loads(route.calls.last.request.content)


@pytest.fixture
def adapter(http, instruments):
    return FyersAdapter(http, instruments)


@pytest.fixture
def session():
    return make_session("fyers", token="fyers-access-token", api_key="XC4XXXXXM-100")


async def test_login_url(adapter):
    url = adapter.login_url(CREDS)
    assert url.startswith(f"{BASE}/generate-authcode?")
    assert "client_id=XC4XXXXXM-100" in url and "response_type=code" in url and "state=kalpi" in url
    assert "redirect_uri=https%3A%2F%2Fapp.example%2Fcb" in url


async def test_login_posts_json_with_app_id_hash(router, adapter):
    route = router.post(f"{BASE}/validate-authcode").respond(200, json=fx("login_ok.json"))
    session = await adapter.complete_login(CREDS)
    assert route.calls.last.request.headers["User-Agent"] == "kalpi-execution-engine/1.0"
    assert body_of(route) == {"grant_type": "authorization_code", "code": "authcode1",
                              "appIdHash": hashlib.sha256(b"XC4XXXXXM-100:secret").hexdigest()}
    assert session.access_token.get_secret_value() == "fyers-access-token"
    assert session.api_key.get_secret_value() == "XC4XXXXXM-100"


async def test_login_failure_is_auth_error(router, adapter):
    router.post(f"{BASE}/validate-authcode").respond(200, json=fx("login_fail.json"))
    with pytest.raises(AuthError):
        await adapter.complete_login(CREDS)


async def test_holdings_split_exchange_and_series(router, adapter, session):
    route = router.get(f"{BASE}/holdings").respond(200, json=fx("holdings.json"))
    holdings = await adapter.get_holdings(session)
    assert route.calls.last.request.headers["Authorization"] == "XC4XXXXXM-100:fyers-access-token"  # no Bearer
    assert [(h.symbol, h.exchange, h.quantity, h.isin) for h in holdings] == [
        ("SBIN", "NSE", 5, "INE062A01020"), ("INFY", "NSE", 10, "INE009A01021")]
    assert holdings[0].average_price == Decimal("620.5") and holdings[0].last_price == Decimal("650.0")


async def test_place_sends_documented_json(router, adapter, session):
    route = router.post(f"{BASE}/orders/sync").respond(200, json=fx("place_ok.json"))
    assert await adapter.place_order(session, make_order(side="SELL")) == "25100500001"
    assert body_of(route) == {"symbol": "NSE:INFY-EQ", "qty": 10, "type": 2, "side": -1, "productType": "CNC",
                              "limitPrice": 0, "stopPrice": 0, "validity": "DAY", "disclosedQty": 0,
                              "offlineOrder": False, "orderTag": "kp1234abcd001"}


async def test_place_errors(router, adapter, session):
    route = router.post(f"{BASE}/orders/sync")
    route.respond(200, json=fx("place_reject.json"))
    with pytest.raises(OrderRejectedError) as info:
        await adapter.place_order(session, make_order())
    assert "margin" in info.value.reason.lower()
    route.respond(500, text="Internal Server Error")
    with pytest.raises(AmbiguousOutcomeError):
        await adapter.place_order(session, make_order())
    route.mock(side_effect=httpx.ConnectTimeout("slow connect"))
    with pytest.raises(BrokerUnavailableError):
        await adapter.place_order(session, make_order())
    route.respond(200, json={"s": "error", "code": -16, "message": "Could not authenticate the user"})
    with pytest.raises(AuthError):
        await adapter.place_order(session, make_order())


async def test_429_carries_retry_after(router, adapter, session):
    router.get(f"{BASE}/orders").respond(429, json=fx("rate_limited.json"),
                                         headers={"Retry-After": "2", "X-Retry-After-Ms": "2000"})
    with pytest.raises(RateLimitError) as info:
        await adapter.list_orders(session)
    assert info.value.retry_after == 2.0
    router.get(f"{BASE}/holdings").respond(429, json=fx("rate_limited.json"), headers={"X-Retry-After-Ms": "750"})
    with pytest.raises(RateLimitError) as info:
        await adapter.get_holdings(session)
    assert info.value.retry_after == 0.75


async def test_read_5xx_and_401(router, adapter, session):
    route = router.get(f"{BASE}/holdings")
    route.respond(503, text="")
    with pytest.raises(BrokerUnavailableError):
        await adapter.get_holdings(session)
    route.respond(401, json={"s": "error", "code": -8, "message": "token expired"})
    with pytest.raises(AuthError):
        await adapter.get_holdings(session)


async def test_get_order_maps_integer_statuses(router, adapter, session):
    route = router.get(f"{BASE}/orders", params={"id": "25100500001"})
    route.respond(200, json=fx("order_open.json"))
    update = await adapter.get_order(session, "25100500001")
    assert (update.status, update.raw_status, update.tag) == (OrderStatus.OPEN, "6", "kp1234abcd001")
    route.respond(200, json=fx("order_complete.json"))
    update = await adapter.get_order(session, "25100500001")
    assert (update.status, update.filled_qty, update.average_price) == (OrderStatus.FILLED, 10, Decimal("1510.25"))
    route.respond(200, json=fx("order_rejected.json"))
    update = await adapter.get_order(session, "25100500001")
    assert update.status is OrderStatus.REJECTED and update.message == "RED:Insufficient funds"
    route.respond(200, json={"s": "ok", "code": 200, "data": fx("order_open.json")["orderBook"]})
    assert (await adapter.get_order(session, "25100500001")).status is OrderStatus.OPEN  # `data` container too


async def test_list_orders_and_find_by_tag(router, adapter, session):
    router.get(f"{BASE}/orders").respond(200, json=fx("order_book.json"))
    book = await adapter.list_orders(session)
    assert [o.status for o in book] == [OrderStatus.OPEN, OrderStatus.FILLED, OrderStatus.CANCELLED,
                                        OrderStatus.PENDING, OrderStatus.CANCELLED]
    found = await adapter.find_order_by_tag(session, "kp1234abcd002")
    assert found is not None and found.broker_order_id == "25100500002"


async def test_find_by_tag_matches_a_prefixed_echo(router, adapter, session):
    row = {**fx("order_book.json")["orderBook"][0], "orderTag": "1:kp1234abcd001"}
    router.get(f"{BASE}/orders").respond(200, json={"s": "ok", "code": 200, "orderBook": [row]})
    found = await adapter.find_order_by_tag(session, "kp1234abcd001")
    assert found is not None and found.broker_order_id == "25100500001"
    assert await adapter.find_order_by_tag(session, "kp1234abcd009") is None


async def test_status_map_covers_every_fixture_status(adapter):
    raw = {row["status"] for name in ("order_open", "order_complete", "order_rejected", "order_book")
           for row in fx(f"{name}.json")["orderBook"]}
    assert raw and all(str(status) in adapter.STATUS_MAP for status in raw)
    assert set(adapter.STATUS_MAP.values()) <= ADAPTER_STATUSES
