"""Contract tests for UpstoxAdapter against recorded fixtures."""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from app.brokers.base import (
    AmbiguousOutcomeError,
    AuthError,
    BrokerUnavailableError,
    OrderRejectedError,
    RateLimitError,
)
from app.brokers.upstox import UpstoxAdapter
from app.core.models import OrderStatus
from tests.adapters.conftest import ADAPTER_STATUSES, load_fixture, make_order, make_session

BASE = "https://api.upstox.com/v2"
ORDERS = "https://api-hft.upstox.com/v3"
CREDS = {"api_key": "upkey", "api_secret": "upsecret", "redirect_uri": "https://app.example/cb", "code": "code1"}
pytestmark = pytest.mark.asyncio


def fx(name: str):
    return load_fixture("upstox", name)


def body_of(route) -> dict:
    return json.loads(route.calls.last.request.content)


@pytest.fixture
def adapter(http, instruments):
    return UpstoxAdapter(http, instruments)


@pytest.fixture
def session():
    return make_session("upstox", token="upstox-access-token", api_key=None)


async def test_login_url(adapter):
    url = adapter.login_url(CREDS)
    assert url.startswith(f"{BASE}/login/authorization/dialog?response_type=code&client_id=upkey")
    assert "redirect_uri=https%3A%2F%2Fapp.example%2Fcb" in url


async def test_login_posts_form_and_reads_bare_token_object(router, adapter, caplog):
    route = router.post(f"{BASE}/login/authorization/token").respond(200, json=fx("login_ok.json"))
    with caplog.at_level(logging.WARNING):
        session = await adapter.complete_login(CREDS)
    request = route.calls.last.request
    assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
    assert {k: v[0] for k, v in parse_qs(request.content.decode()).items()} == {
        "code": "code1", "client_id": "upkey", "client_secret": "upsecret",
        "redirect_uri": "https://app.example/cb", "grant_type": "authorization_code"}
    assert session.access_token.get_secret_value() == "upstox-access-token" and session.user_id == "UPX123"
    assert "poa" in caplog.text.lower()  # poa:false: sells may need eDIS
    assert "upsecret" not in caplog.text and "upstox-access-token" not in caplog.text


async def test_login_failure_is_auth_error(router, adapter):
    router.post(f"{BASE}/login/authorization/token").respond(400, json=fx("login_fail.json"))
    with pytest.raises(AuthError):
        await adapter.complete_login(CREDS)


async def test_holdings_map_to_canonical(router, adapter, session):
    route = router.get(f"{BASE}/portfolio/long-term-holdings").respond(200, json=fx("holdings.json"))
    holdings = await adapter.get_holdings(session)
    headers = route.calls.last.request.headers
    assert headers["Authorization"] == "Bearer upstox-access-token" and headers["Accept"] == "application/json"
    assert [(h.symbol, h.exchange, h.quantity, h.isin) for h in holdings] == [
        ("INFY", "NSE", 10, "INE009A01021"), ("SBIN", "NSE", 5, "INE062A01020")]
    assert holdings[1].average_price == Decimal("620.5") and holdings[1].last_price == Decimal("650.0")


async def test_place_goes_to_hft_host_with_isin_instrument_token(router, adapter, session):
    route = router.post(f"{ORDERS}/order/place").respond(200, json=fx("place_ok.json"))
    assert await adapter.place_order(session, make_order()) == "1644490272000"
    assert body_of(route) == {"quantity": 10, "product": "D", "validity": "DAY", "price": 0, "tag": "kp1234abcd001",
                              "instrument_token": "NSE_EQ|INE009A01021", "order_type": "MARKET",
                              "transaction_type": "BUY", "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False}
    route.respond(200, json=fx("place_ok_v3.json"))
    assert await adapter.place_order(session, make_order()) == "1644490272000"  # v3 order_ids[] accepted
    await adapter.place_order(session, make_order(symbol="TCS", isin=None))
    assert body_of(route)["instrument_token"] == "NSE_EQ|INE467B01029"  # isin looked up from the table


async def test_place_errors(router, adapter, session):
    route = router.post(f"{ORDERS}/order/place")
    route.respond(400, json=fx("place_reject.json"))
    with pytest.raises(OrderRejectedError) as info:
        await adapter.place_order(session, make_order())
    assert info.value.reason == "Insufficient funds"
    route.respond(429, json=fx("rate_limited.json"))
    with pytest.raises(RateLimitError):
        await adapter.place_order(session, make_order())
    route.respond(400, json={"status": "error", "errors": [{"errorCode": "UDAPI100050", "message": "Invalid token"}]})
    with pytest.raises(AuthError):
        await adapter.place_order(session, make_order())
    route.respond(503, text="")
    with pytest.raises(AmbiguousOutcomeError):
        await adapter.place_order(session, make_order())
    route.mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(BrokerUnavailableError):
        await adapter.place_order(session, make_order())


async def test_place_accepted_without_an_order_id_is_ambiguous(router, adapter, session):
    route = router.post(f"{ORDERS}/order/place")
    route.respond(200, json={"status": "success", "data": {"order_identifier": "1644490272000"}})
    with pytest.raises(AmbiguousOutcomeError):
        await adapter.place_order(session, make_order())
    route.respond(200, json={"status": "success", "data": None})
    with pytest.raises(AmbiguousOutcomeError):
        await adapter.place_order(session, make_order())


async def test_read_errors(router, adapter, session):
    route = router.get(f"{BASE}/order/retrieve-all")
    route.respond(500, text="")
    with pytest.raises(BrokerUnavailableError):
        await adapter.list_orders(session)
    route.respond(401, json=fx("login_fail.json"))
    with pytest.raises(AuthError):
        await adapter.list_orders(session)


async def test_get_order_details(router, adapter, session):
    route = router.get(f"{BASE}/order/details", params={"order_id": "1644490272000"})
    route.respond(200, json=fx("order_open.json"))
    update = await adapter.get_order(session, "1644490272000")
    assert (update.status, update.tag, update.raw_status) == (OrderStatus.OPEN, "kp1234abcd001", "open")
    route.respond(200, json=fx("order_complete.json"))
    update = await adapter.get_order(session, "1644490272000")
    assert (update.status, update.filled_qty, update.average_price) == (OrderStatus.FILLED, 10, Decimal("1510.25"))
    route.respond(200, json=fx("order_rejected.json"))
    update = await adapter.get_order(session, "1644490272000")
    assert update.status is OrderStatus.REJECTED and update.message.startswith("RMS:")


async def test_list_orders_and_find_by_tag(router, adapter, session):
    router.get(f"{BASE}/order/retrieve-all").respond(200, json=fx("order_book.json"))
    book = await adapter.list_orders(session)
    assert [o.status for o in book] == [OrderStatus.OPEN, OrderStatus.FILLED, OrderStatus.CANCELLED,
                                        OrderStatus.PENDING]
    assert [o.tag for o in book] == ["kp1234abcd001", "kp1234abcd002", "kp1234abcd003", None]
    found = await adapter.find_order_by_tag(session, "kp1234abcd002")
    assert found is not None and found.broker_order_id == "1644490272001"


async def test_base_urls_are_config_overridable_for_sandbox(router, http, instruments, session):
    sandbox = "https://api-sandbox.upstox.com/v2"
    adapter = UpstoxAdapter(http, instruments, {"base_url": sandbox, "order_base_url": sandbox})
    route = router.post(f"{sandbox}/order/place").respond(200, json=fx("place_ok.json"))
    await adapter.place_order(session, make_order())
    assert route.called and adapter.login_url(CREDS).startswith(sandbox)


async def test_status_map_covers_every_fixture_status(adapter):
    raw = {fx(f"{n}.json")["data"]["status"] for n in ("order_open", "order_complete", "order_rejected")}
    raw |= {row["status"] for row in fx("order_book.json")["data"]}
    assert raw and all(status.lower() in adapter.STATUS_MAP for status in raw)
    assert set(adapter.STATUS_MAP.values()) <= ADAPTER_STATUSES
