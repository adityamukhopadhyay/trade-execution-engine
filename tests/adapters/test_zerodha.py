"""Contract tests for ZerodhaAdapter against recorded fixtures."""
from __future__ import annotations

import hashlib
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
from app.brokers.zerodha import ZerodhaAdapter
from app.core.models import OrderStatus
from tests.adapters.conftest import ADAPTER_STATUSES, load_fixture, make_order, make_session

BASE = "https://api.kite.trade"
CREDS = {"api_key": "kitekey", "api_secret": "kitesecret", "request_token": "rt123"}
pytestmark = pytest.mark.asyncio


def fx(name: str):
    return load_fixture("zerodha", name)


def form_of(route) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(route.calls.last.request.content.decode()).items()}


@pytest.fixture
def adapter(http, instruments):
    return ZerodhaAdapter(http, instruments)


@pytest.fixture
def session():
    return make_session("zerodha", token="zerodha-access-token-xyz", api_key="kitekey")


async def test_login_url(adapter):
    assert adapter.login_url(CREDS) == "https://kite.zerodha.com/connect/login?v=3&api_key=kitekey"


async def test_login_posts_form_with_sha256_checksum(router, adapter):
    route = router.post(f"{BASE}/session/token").respond(200, json=fx("login_ok.json"))
    session = await adapter.complete_login(CREDS)
    request = route.calls.last.request
    assert request.headers["X-Kite-Version"] == "3"
    assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
    expected = hashlib.sha256(b"kitekeyrt123kitesecret").hexdigest()  # api_key + request_token + api_secret
    assert form_of(route) == {"api_key": "kitekey", "request_token": "rt123", "checksum": expected}
    assert session.access_token.get_secret_value() == "zerodha-access-token-xyz"
    assert session.api_key.get_secret_value() == "kitekey" and session.user_id == "AB1234"
    assert session.expires_at > session.created_at


async def test_login_failure_is_auth_error(router, adapter):
    router.post(f"{BASE}/session/token").respond(403, json=fx("login_fail.json"))
    with pytest.raises(AuthError):
        await adapter.complete_login(CREDS)
    with pytest.raises(AuthError):
        await adapter.complete_login({"api_key": "k"})  # missing fields never hit the wire


async def test_holdings_map_to_canonical(router, adapter, session):
    route = router.get(f"{BASE}/portfolio/holdings").respond(200, json=fx("holdings.json"))
    holdings = await adapter.get_holdings(session)
    assert route.calls.last.request.headers["Authorization"] == "token kitekey:zerodha-access-token-xyz"
    assert [(h.symbol, h.exchange, h.quantity, h.isin) for h in holdings] == [
        ("INFY", "NSE", 10, "INE009A01021"), ("SBIN", "NSE", 3, "INE062A01020")]  # SBIN: 5 held, 2 used
    assert holdings[0].average_price == Decimal("1450.5") and holdings[0].last_price == Decimal("1510.25")


async def test_place_sends_exact_form_fields_and_tag(router, adapter, session):
    route = router.post(f"{BASE}/orders/regular").respond(200, json=fx("place_ok.json"))
    order_id = await adapter.place_order(session, make_order())
    assert order_id == "151220000000000"
    assert form_of(route) == {"tradingsymbol": "INFY", "exchange": "NSE", "transaction_type": "BUY",
                              "order_type": "MARKET", "quantity": "10", "product": "CNC", "validity": "DAY",
                              "tag": "kp1234abcd001"}


async def test_place_sync_reject(router, adapter, session):
    router.post(f"{BASE}/orders/regular").respond(400, json=fx("place_reject.json"))
    with pytest.raises(OrderRejectedError) as info:
        await adapter.place_order(session, make_order())
    assert "Insufficient funds" in info.value.reason


async def test_place_transport_split(router, adapter, session):
    route = router.post(f"{BASE}/orders/regular")
    route.respond(502, text="Bad Gateway")
    with pytest.raises(AmbiguousOutcomeError):
        await adapter.place_order(session, make_order())
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(AmbiguousOutcomeError):
        await adapter.place_order(session, make_order())
    route.mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(BrokerUnavailableError):
        await adapter.place_order(session, make_order())
    route.mock(side_effect=httpx.UnsupportedProtocol("bad scheme"))
    with pytest.raises(BrokerUnavailableError):
        await adapter.place_order(session, make_order())


async def test_read_errors(router, adapter, session):
    route = router.get(f"{BASE}/portfolio/holdings")
    route.respond(503, text="Service Unavailable")
    with pytest.raises(BrokerUnavailableError):
        await adapter.get_holdings(session)
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(BrokerUnavailableError):  # reads: retryable, never ambiguous
        await adapter.get_holdings(session)
    route.respond(429, json=fx("rate_limited.json"))
    with pytest.raises(RateLimitError) as info:
        await adapter.get_holdings(session)
    assert info.value.retry_after is None  # Kite documents no Retry-After header
    route.respond(403, json=fx("login_fail.json"))
    with pytest.raises(AuthError):
        await adapter.get_holdings(session)


async def test_get_order_uses_last_history_row(router, adapter, session):
    route = router.get(f"{BASE}/orders/151220000000000")
    route.respond(200, json=fx("order_open.json"))
    update = await adapter.get_order(session, "151220000000000")
    assert (update.status, update.filled_qty, update.average_price, update.tag) == (
        OrderStatus.OPEN, 0, None, "kp1234abcd001")
    route.respond(200, json=fx("order_complete.json"))
    update = await adapter.get_order(session, "151220000000000")
    assert (update.status, update.filled_qty, update.average_price) == (OrderStatus.FILLED, 10, Decimal("1510.25"))
    route.respond(200, json=fx("order_rejected.json"))
    update = await adapter.get_order(session, "151220000000000")
    assert update.status is OrderStatus.REJECTED and "Insufficient funds" in update.message


async def test_list_orders_and_find_by_tag(router, adapter, session):
    router.get(f"{BASE}/orders").respond(200, json=fx("order_book.json"))
    book = await adapter.list_orders(session)
    assert [o.tag for o in book][:3] == ["kp1234abcd001", "kp1234abcd002", "kp1234abcd003"]
    cancelled = book[2]
    assert cancelled.status is OrderStatus.CANCELLED and cancelled.filled_qty == 4  # partial then cancelled
    found = await adapter.find_order_by_tag(session, "kp1234abcd002")
    assert found is not None and found.status is OrderStatus.FILLED and found.broker_order_id == "151220000000001"
    assert await adapter.find_order_by_tag(session, "kp9999999999") is None


async def test_status_map_covers_every_fixture_status(adapter):
    raw = {row["status"] for name in ("order_open", "order_complete", "order_rejected", "order_book")
           for row in fx(f"{name}.json")["data"]}
    assert raw and all(status.lower() in adapter.STATUS_MAP for status in raw)
    assert set(adapter.STATUS_MAP.values()) <= ADAPTER_STATUSES
