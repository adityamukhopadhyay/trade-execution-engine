"""PaperBroker behaviour: holdings seeding, fills, rejections and every fault knob, with zero latency."""
from __future__ import annotations

import pytest

from app.brokers.base import (
    AmbiguousOutcomeError,
    BrokerUnavailableError,
    OrderRejectedError,
    RateLimitError,
    SymbolNotFoundError,
)
from app.brokers.paper import PaperBroker, PaperFaults, parse_faults, price
from app.core.instruments import Instrument, InstrumentTable
from app.core.models import OrderRequest, OrderSide, OrderStatus

pytestmark = pytest.mark.asyncio
TABLE = InstrumentTable([
    Instrument("INFY", "NSE", "INE009A01021", "Infosys", "1594"),
    Instrument("TCS", "NSE", "INE467B01029", "TCS", "11536"),
    Instrument("SBIN", "NSE", "INE062A01020", "SBI", "3045"),
    Instrument("REJECTME", "NSE", "INE000000000", "fake", "0"),
])


def order(symbol: str, side: str = "BUY", qty: int = 10, tag: str = "kp0000000a001") -> OrderRequest:
    return OrderRequest(tag=tag, symbol=symbol, exchange="NSE", isin=None, side=OrderSide(side), quantity=qty)


async def login(broker: PaperBroker, **knobs: str):
    return await broker.complete_login({"latency_ms": "0-0", **knobs})


@pytest.fixture
def broker():
    return PaperBroker(None, TABLE)


async def test_login_gives_a_fresh_empty_account(broker):
    session = await login(broker)
    assert session.broker == "paper" and session.session_id in broker.accounts
    assert await broker.get_holdings(session) == []
    assert session.access_token.get_secret_value() == "paper" and not session.is_expired


async def test_seed_holdings_parsed_and_junk_rejected(broker):
    session = await login(broker, seed_holdings="INFY:10, tcs:5")
    holdings = await broker.get_holdings(session)
    assert [(h.symbol, h.quantity, h.isin) for h in holdings] == [("INFY", 10, "INE009A01021"),
                                                                   ("TCS", 5, "INE467B01029")]
    with pytest.raises(SymbolNotFoundError):
        await login(broker, seed_holdings="NOPE:3")


async def test_env_config_is_the_default_and_credentials_override(monkeypatch):
    broker = PaperBroker(None, TABLE, {"seed_holdings": "SBIN:7", "faults": {"latency_ms": "0-0", "seed": "9"}})
    session = await broker.complete_login({})
    assert [(h.symbol, h.quantity) for h in await broker.get_holdings(session)] == [("SBIN", 7)]
    assert broker.accounts[session.session_id].faults == PaperFaults(latency_ms=(0, 0), seed=9)
    session = await broker.complete_login({"seed_holdings": "INFY:1", "seed": "3", "ambiguous_placed": "false"})
    assert [(h.symbol, h.quantity) for h in await broker.get_holdings(session)] == [("INFY", 1)]
    assert broker.accounts[session.session_id].faults == PaperFaults(latency_ms=(0, 0), seed=3, ambiguous_placed=False)


async def test_parse_faults_coerces_strings():
    faults = parse_faults({}, {"reject_rate": "0.2", "latency_ms": "5-9", "rate_limit_every_n": "7",
                               "ambiguous_placed": "True", "fill_after_polls": "2"})
    assert faults == PaperFaults(reject_rate=0.2, latency_ms=(5, 9), rate_limit_every_n=7, fill_after_polls=2)


async def test_buy_fills_after_n_polls_and_updates_holdings(broker):
    session = await login(broker, fill_after_polls="2")
    broker_id = await broker.place_order(session, order("INFY"))
    assert broker_id == "P000001"
    first = await broker.get_order(session, broker_id)
    assert first.status is OrderStatus.OPEN and first.filled_qty == 0 and first.tag == "kp0000000a001"
    assert await broker.get_holdings(session) == []
    second = await broker.get_order(session, broker_id)
    assert second.status is OrderStatus.FILLED and second.filled_qty == 10
    assert second.average_price == price("INFY") and second.raw_status == "FILLED"
    holdings = await broker.get_holdings(session)
    assert [(h.symbol, h.quantity, h.isin) for h in holdings] == [("INFY", 10, "INE009A01021")]


async def test_sell_reduces_then_removes_and_over_sell_is_rejected(broker):
    session = await login(broker, seed_holdings="INFY:10")
    with pytest.raises(OrderRejectedError) as info:
        await broker.place_order(session, order("INFY", "SELL", 11))
    assert info.value.reason == "Holdings not available for sell"
    with pytest.raises(OrderRejectedError):
        await broker.place_order(session, order("TCS", "SELL", 1))  # not held at all
    sell = await broker.place_order(session, order("INFY", "SELL", 4))
    await broker.get_order(session, sell)
    assert [(h.symbol, h.quantity) for h in await broker.get_holdings(session)] == [("INFY", 6)]
    sell = await broker.place_order(session, order("INFY", "SELL", 6, tag="kp0000000a002"))
    await broker.get_order(session, sell)
    assert await broker.get_holdings(session) == []


async def test_rejectme_is_accepted_then_rejected_on_poll(broker):
    session = await login(broker)
    broker_id = await broker.place_order(session, order("REJECTME"))
    update = await broker.get_order(session, broker_id)
    assert update.status is OrderStatus.REJECTED and update.message == "simulated RMS rejection"
    assert await broker.get_holdings(session) == []


async def test_reject_rate_one_is_a_sync_reject(broker):
    session = await login(broker, reject_rate="1")
    with pytest.raises(OrderRejectedError):
        await broker.place_order(session, order("INFY"))
    assert broker.accounts[session.session_id].orders == {}


async def test_rate_limit_every_n_is_not_recorded(broker):
    session = await login(broker, rate_limit_every_n="2")
    await broker.place_order(session, order("INFY"))
    with pytest.raises(RateLimitError) as info:
        await broker.place_order(session, order("TCS", tag="kp0000000a002"))
    assert info.value.retry_after == 0.05
    account = broker.accounts[session.session_id]
    assert len(account.orders) == 1 and account.place_calls == 2 and account.orders_received == 1
    await broker.place_order(session, order("TCS", tag="kp0000000a002"))  # retry is not a duplicate
    assert len(account.orders) == 2 and account.duplicate_tags == 0


async def test_ambiguous_placed_true_records_the_order(broker):
    session = await login(broker, ambiguous_rate="1", ambiguous_placed="true")
    with pytest.raises(AmbiguousOutcomeError):
        await broker.place_order(session, order("INFY"))
    found = await broker.find_order_by_tag(session, "kp0000000a001")
    assert found is not None and found.status is OrderStatus.FILLED  # list_orders advanced it
    assert len(broker.accounts[session.session_id].orders) == 1


async def test_ambiguous_placed_false_drops_the_order(broker):
    session = await login(broker, ambiguous_rate="1", ambiguous_placed="false")
    with pytest.raises(AmbiguousOutcomeError):
        await broker.place_order(session, order("INFY"))
    assert await broker.find_order_by_tag(session, "kp0000000a001") is None
    assert broker.accounts[session.session_id].orders == {}


async def test_duplicate_tag_is_counted(broker):
    session = await login(broker)
    await broker.place_order(session, order("INFY"))
    await broker.place_order(session, order("INFY"))
    assert broker.accounts[session.session_id].duplicate_tags == 1


async def test_partial_fill_stays_open_at_half(broker):
    session = await login(broker, partial_fill_rate="1")
    broker_id = await broker.place_order(session, order("INFY", qty=9))
    for _ in range(3):
        update = await broker.get_order(session, broker_id)
        assert update.status is OrderStatus.OPEN and update.filled_qty == 4
    assert [(h.symbol, h.quantity) for h in await broker.get_holdings(session)] == [("INFY", 4)]


async def test_partial_fill_of_a_single_share_fills_outright(broker):
    session = await login(broker, partial_fill_rate="1")
    update = await broker.get_order(session, await broker.place_order(session, order("INFY", qty=1)))
    assert update.status is OrderStatus.FILLED and update.filled_qty == 1


async def test_list_orders_advances_every_open_order(broker):
    session = await login(broker)
    ids = [await broker.place_order(session, order(s, tag=f"kp0000000a00{i}")) for i, s in enumerate(("INFY", "TCS"))]
    book = await broker.list_orders(session)
    assert [o.broker_order_id for o in book] == ids
    assert all(o.status is OrderStatus.FILLED and o.tag for o in book)
    with pytest.raises(BrokerUnavailableError):
        await broker.get_order(session, "P999999")


async def test_same_seed_same_sequence():
    outcomes = []
    for _ in range(2):
        broker = PaperBroker(None, TABLE)
        session = await login(broker, reject_rate="0.5", seed="11")
        result = []
        for i in range(12):
            try:
                result.append(await broker.place_order(session, order("INFY", tag=f"kp0000000a{i:03d}")))
            except OrderRejectedError:
                result.append("REJECT")
        outcomes.append(result)
    assert outcomes[0] == outcomes[1] and "REJECT" in outcomes[0] and "P000001" in outcomes[0]
