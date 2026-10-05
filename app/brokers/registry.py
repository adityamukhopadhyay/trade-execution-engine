"""Broker registry: one module per broker plus one entry in ADAPTERS."""
from __future__ import annotations

from typing import Any

import httpx

from app.brokers.angelone import AngelOneAdapter
from app.brokers.base import BrokerAdapter
from app.brokers.fyers import FyersAdapter
from app.brokers.groww import GrowwAdapter
from app.brokers.paper import PaperBroker
from app.brokers.upstox import UpstoxAdapter
from app.brokers.zerodha import ZerodhaAdapter
from app.core.errors import UnknownBrokerError
from app.core.instruments import InstrumentTable
from app.core.models import BrokerInfo

ADAPTERS: tuple[type[BrokerAdapter], ...] = (
    PaperBroker, ZerodhaAdapter, FyersAdapter, AngelOneAdapter, UpstoxAdapter, GrowwAdapter,
)
REGISTRY: dict[str, type[BrokerAdapter]] = {cls.meta.name: cls for cls in ADAPTERS}


def get_adapter_class(name: str) -> type[BrokerAdapter]:
    try:
        return REGISTRY[name]
    except KeyError:
        raise UnknownBrokerError(f"unknown broker '{name}'; known: {sorted(REGISTRY)}") from None


class BrokerRegistry:
    """One adapter instance per registered broker, built once at startup and shared by every session."""

    def __init__(self, http: httpx.AsyncClient, instruments: InstrumentTable,
                 configs: dict[str, dict[str, Any]] | None = None) -> None:
        configs = configs or {}
        self._adapters: dict[str, BrokerAdapter] = {
            name: cls(http, instruments, configs.get(name)) for name, cls in REGISTRY.items()
        }

    def get(self, name: str) -> BrokerAdapter:
        try:
            return self._adapters[name]
        except KeyError:
            raise UnknownBrokerError(f"unknown broker '{name}'; known: {sorted(self._adapters)}") from None

    def describe(self) -> list[BrokerInfo]:
        return [a.meta.info() for a in self._adapters.values()]
