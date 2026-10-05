"""The registry exposes the six built-in brokers, and a 6th-party broker is one class + one dict entry."""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from app.brokers.base import BrokerAdapter, BrokerMeta, RateLimits
from app.brokers.registry import REGISTRY, BrokerRegistry, get_adapter_class
from app.core.errors import UnknownBrokerError
from app.core.instruments import InstrumentTable
from app.core.models import BrokerSession, OrderStatus, OrderUpdate, utcnow

BUILT_IN = {"paper", "zerodha", "fyers", "angelone", "upstox", "groww"}


class AcmeAdapter(BrokerAdapter):
    """A complete 6th broker in a few lines: metadata plus the five abstract methods."""

    meta = BrokerMeta(name="acme", display_name="Acme Securities", required_credentials=("token",),
                      limits=RateLimits(5, 100, 5))
    BASE_URL = "https://api.acme.example"

    async def complete_login(self, credentials):
        return BrokerSession(session_id="acme-1", broker="acme", access_token=SecretStr(credentials["token"]),
                             expires_at=utcnow() + timedelta(hours=1))

    async def get_holdings(self, session):
        return []

    async def get_order(self, session, broker_order_id):
        return OrderUpdate(broker_order_id=broker_order_id, status=OrderStatus.FILLED)

    async def list_orders(self, session):
        return []

    async def place_order(self, session, order):
        return "ACME-1"


@pytest.fixture
def registry_kwargs():
    return {"http": httpx.AsyncClient(), "instruments": InstrumentTable([])}


def test_six_built_in_brokers(registry_kwargs):
    assert set(REGISTRY) == BUILT_IN
    registry = BrokerRegistry(**registry_kwargs)
    described = {info.name: info for info in registry.describe()}
    assert set(described) == BUILT_IN
    assert described["paper"].live_tested is True
    assert all(described[name].live_tested is False for name in BUILT_IN - {"paper"})
    assert described["zerodha"].login_via_redirect is True and described["angelone"].login_via_redirect is False


def test_unknown_broker(registry_kwargs):
    with pytest.raises(UnknownBrokerError):
        get_adapter_class("dhan")
    with pytest.raises(UnknownBrokerError):
        BrokerRegistry(**registry_kwargs).get("dhan")


def test_config_reaches_the_adapter(registry_kwargs):
    registry = BrokerRegistry(**registry_kwargs, configs={"zerodha": {"base_url": "https://sandbox.kite.example"}})
    assert registry.get("zerodha").base_url == "https://sandbox.kite.example"
    assert registry.get("fyers").base_url == "https://api-t1.fyers.in/api/v3"


def test_sixth_broker_in_one_file(monkeypatch, registry_kwargs):
    monkeypatch.setitem(REGISTRY, "acme", AcmeAdapter)
    assert get_adapter_class("acme") is AcmeAdapter
    registry = BrokerRegistry(**registry_kwargs)
    acme = registry.get("acme")
    assert isinstance(acme, AcmeAdapter) and acme.meta.name == "acme"
    assert acme.limiter is not None and acme.base_url == "https://api.acme.example"
    names = [info.name for info in registry.describe()]
    assert "acme" in names and set(names) == BUILT_IN | {"acme"}
