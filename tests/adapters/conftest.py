"""Shared pieces for the adapter contract tests: respx router, http client, instruments, fixture loader."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.core.instruments import Instrument, InstrumentTable
from app.core.models import BrokerSession, OrderRequest, OrderSide, OrderStatus, utcnow

FIXTURES = Path(__file__).parent / "fixtures"
ADAPTER_STATUSES = {OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.FILLED, OrderStatus.REJECTED,
                    OrderStatus.CANCELLED}


def load_fixture(broker: str, name: str) -> Any:
    """The recorded body: a dict for .json files (the `_source` note stripped), the raw text for .txt."""
    path = FIXTURES / broker / name
    if path.suffix == ".txt":
        return path.read_text()
    body = json.loads(path.read_text())
    body.pop("_source", None)
    return body


def make_session(broker: str, token: str = "access-token", api_key: str | None = "api-key") -> BrokerSession:
    return BrokerSession(session_id="sess-1", broker=broker, user_id="U1", access_token=SecretStr(token),
                         api_key=SecretStr(api_key) if api_key else None, expires_at=utcnow() + timedelta(hours=8))


def make_order(symbol: str = "INFY", side: str = "BUY", quantity: int = 10, tag: str = "kp1234abcd001",
               isin: str | None = "INE009A01021") -> OrderRequest:
    return OrderRequest(tag=tag, symbol=symbol, exchange="NSE", isin=isin, side=OrderSide(side), quantity=quantity)


@pytest.fixture
def instruments() -> InstrumentTable:
    return InstrumentTable([
        Instrument("INFY", "NSE", "INE009A01021", "Infosys", "1594"),
        Instrument("TCS", "NSE", "INE467B01029", "TCS", "11536"),
        Instrument("SBIN", "NSE", "INE062A01020", "SBI", "3045"),
        Instrument("RELIANCE", "NSE", "INE002A01018", "Reliance", "2885"),
        Instrument("TATASTEEL", "NSE", "INE081A01020", "Tata Steel", "3499"),
        Instrument("NOTOKEN", "NSE", "INE000000001", "No Angel token", None),
    ])


@pytest.fixture
def http() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": "kalpi-execution-engine/1.0"},
                             timeout=httpx.Timeout(5.0, connect=2.0))


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        yield mock
