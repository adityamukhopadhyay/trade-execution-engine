"""Contract tests for GrowwAdapter against recorded fixtures."""
from __future__ import annotations

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
    SymbolNotFoundError,
)
from app.brokers.groww import GrowwAdapter
from app.core.models import OrderStatus
from tests.adapters.conftest import ADAPTER_STATUSES, load_fixture, make_order, make_session

BASE = "https://api.groww.in/v1"
pytestmark = pytest.mark.asyncio


def fx(name: str):
    return load_fixture("groww", name)


def body_of(route) -> dict:
    return json.loads(route.calls.last.request.content)


@pytest.fixture
def adapter(http, instruments):
    return GrowwAdapter(http, instruments)


@pytest.fixture
def session():
    return make_session("groww", token="groww-access-token", api_key=None)


async def test_no_login_url(adapter):
    assert adapter.login_url({}) is None


async def test_pasted_token_is_verified_with_holdings_not_the_token_endpoint(router, adapter):
    token_route = router.post(f"{BASE}/token/api/access").respond(200, json=fx("login_ok.json"))
    verify = router.get(f"{BASE}/holdings/user").respond(200, json=fx("holdings.json"))
    session = await adapter.complete_login({"access_token": "pasted-token"})
    assert session.access_token.get_secret_value() == "pasted-token"
    assert verify.calls.last.request.headers["Authorization"] == "Bearer pasted-token"
    assert verify.calls.last.request.headers["X-API-VERSION"] == "1.0"
    assert not token_route.called  # token endpoint capped at 150/day


async def test_api_key_plus_totp_exchanges_for_a_token(router, adapter):
    token_route = router.post(f"{BASE}/token/api/access").respond(200, json=fx("login_ok.json"))
    router.get(f"{BASE}/holdings/user").respond(200, json=fx("holdings.json"))
    session = await adapter.complete_login({"api_key": "growwkey", "totp": "123456"})
    assert token_route.calls.last.request.headers["Authorization"] == "Bearer growwkey"
    assert body_of(token_route) == {"key_type": "totp", "totp": "123456"}
    assert session.access_token.get_secret_value() == "groww-access-token"


async def test_login_failures(router, adapter):
    with pytest.raises(AuthError):
        await adapter.complete_login({})  # neither combo given
    router.post(f"{BASE}/token/api/access").respond(400, json=fx("login_fail.json"))
    with pytest.raises(AuthError):
        await adapter.complete_login({"api_key": "growwkey", "totp": "000000"})
    router.get(f"{BASE}/holdings/user").respond(401, json={"status": "FAILURE", "error": {"code": "GA002"}})
    with pytest.raises(AuthError):
        await adapter.complete_login({"access_token": "stale"})


async def test_holdings_assume_nse_and_use_free_quantity(router, adapter, session):
    router.get(f"{BASE}/holdings/user").respond(200, json=fx("holdings.json"))
    holdings = await adapter.get_holdings(session)
    assert [(h.symbol, h.exchange, h.quantity, h.isin) for h in holdings] == [
        ("RELIANCE", "NSE", 4, "INE002A01018"), ("INFY", "NSE", 8, "INE009A01021")]  # 10 held, 2 pledged
    assert holdings[0].average_price == Decimal("2450.75") and holdings[0].last_price is None


async def test_place_sends_reference_id_as_tag(router, adapter, session):
    route = router.post(f"{BASE}/order/create").respond(200, json=fx("place_ok.json"))
    assert await adapter.place_order(session, make_order()) == "GMK39038RDT490CCVRO"
    assert body_of(route) == {"trading_symbol": "INFY", "exchange": "NSE", "segment": "CASH", "quantity": 10,
                              "validity": "DAY", "product": "CNC", "order_type": "MARKET", "transaction_type": "BUY",
                              "order_reference_id": "kp1234abcd001"}


async def test_place_errors(router, adapter, session):
    route = router.post(f"{BASE}/order/create")
    route.respond(400, json=fx("place_reject.json"))
    with pytest.raises(OrderRejectedError) as info:
        await adapter.place_order(session, make_order())
    assert "Insufficient funds" in info.value.reason
    route.respond(400, json=fx("place_bad_symbol.json"))
    with pytest.raises(SymbolNotFoundError):
        await adapter.place_order(session, make_order())
    route.respond(429, json=fx("rate_limited.json"))
    with pytest.raises(RateLimitError):
        await adapter.place_order(session, make_order())
    route.respond(403, json={"status": "FAILURE", "error": {"code": "GA002", "message": "Unauthorized"}})
    with pytest.raises(AuthError):
        await adapter.place_order(session, make_order())
    route.respond(502, text="Bad Gateway")
    with pytest.raises(AmbiguousOutcomeError):
        await adapter.place_order(session, make_order())
    route.mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(BrokerUnavailableError):
        await adapter.place_order(session, make_order())


async def test_read_5xx_is_unavailable(router, adapter, session):
    router.get(f"{BASE}/order/list", params={"segment": "CASH"}).respond(503, text="")
    with pytest.raises(BrokerUnavailableError):
        await adapter.list_orders(session)


async def test_get_order_detail(router, adapter, session):
    route = router.get(f"{BASE}/order/detail/GMK39038RDT490CCVRO", params={"segment": "CASH"})
    route.respond(200, json=fx("order_open.json"))
    update = await adapter.get_order(session, "GMK39038RDT490CCVRO")
    assert (update.status, update.tag, update.raw_status) == (OrderStatus.OPEN, "kp1234abcd001", "OPEN")
    route.respond(200, json=fx("order_complete.json"))
    update = await adapter.get_order(session, "GMK39038RDT490CCVRO")
    assert (update.status, update.filled_qty, update.average_price) == (OrderStatus.FILLED, 10, Decimal("1510.25"))
    route.respond(200, json=fx("order_rejected.json"))
    update = await adapter.get_order(session, "GMK39038RDT490CCVRO")
    assert update.status is OrderStatus.REJECTED and update.message == "Insufficient funds"


async def test_list_orders(router, adapter, session):
    route = router.get(f"{BASE}/order/list").respond(200, json=fx("order_book.json"))
    book = await adapter.list_orders(session)
    assert dict(route.calls.last.request.url.params) == {"segment": "CASH", "page": "0", "page_size": "100"}
    assert [o.status for o in book] == [OrderStatus.OPEN, OrderStatus.FILLED, OrderStatus.CANCELLED,
                                        OrderStatus.FILLED, OrderStatus.PENDING]
    assert [o.tag for o in book][:3] == ["kp1234abcd001", "kp1234abcd002", "kp1234abcd003"]


async def test_find_by_tag_uses_reference_endpoint_then_falls_back(router, adapter, session):
    by_ref = router.get(f"{BASE}/order/status/reference/kp1234abcd001", params={"segment": "CASH"})
    by_ref.respond(200, json=fx("order_by_reference.json"))
    listing = router.get(f"{BASE}/order/list").respond(200, json=fx("order_book.json"))
    found = await adapter.find_order_by_tag(session, "kp1234abcd001")
    assert found is not None and found.broker_order_id == "GMK39038RDT490CCVRO" and not listing.called
    by_ref.respond(404, json={"status": "FAILURE", "error": {"code": "GA004", "message": "Order not found"}})
    found = await adapter.find_order_by_tag(session, "kp1234abcd001")
    assert found is not None and found.status is OrderStatus.OPEN and listing.called
    missing = router.get(f"{BASE}/order/status/reference/kp9999999999").respond(404, json={"status": "FAILURE"})
    assert await adapter.find_order_by_tag(session, "kp9999999999") is None and missing.called


async def test_status_map_covers_every_fixture_status(adapter):
    raw = {fx(f"{n}.json")["payload"]["order_status"] for n in ("order_open", "order_complete", "order_rejected")}
    raw |= {row["order_status"] for row in fx("order_book.json")["payload"]["order_list"]}
    assert raw and all(status.lower() in adapter.STATUS_MAP for status in raw)
    assert set(adapter.STATUS_MAP.values()) <= ADAPTER_STATUSES
