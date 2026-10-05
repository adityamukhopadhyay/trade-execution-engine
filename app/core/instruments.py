"""Canonical (symbol, exchange) identity plus the per-broker keys that hang off it."""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import SymbolNotFoundError


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    exchange: str  # "NSE" | "BSE"
    isin: str
    name: str
    angel_token: str | None = None  # SmartAPI symboltoken

    @property
    def key(self) -> tuple[str, str]:
        return (self.symbol, self.exchange)


class InstrumentTable:
    """Read-only lookup. Unknown symbol -> SymbolNotFoundError (HTTP 422 at the API edge)."""

    def __init__(self, rows: Iterable[Instrument]) -> None:
        self._by_key: dict[tuple[str, str], Instrument] = {r.key: r for r in rows}
        self._by_isin: dict[tuple[str, str], Instrument] = {(r.isin, r.exchange): r for r in self._by_key.values()}

    @classmethod
    def load(cls, path: str | Path) -> InstrumentTable:
        return cls(Instrument(**row) for row in json.loads(Path(path).read_text()))

    def get(self, symbol: str, exchange: str = "NSE") -> Instrument:
        try:
            return self._by_key[(symbol.upper(), exchange)]
        except KeyError:
            raise SymbolNotFoundError(f"{symbol} is not in the instrument table for {exchange}") from None

    def by_isin(self, isin: str, exchange: str = "NSE") -> Instrument | None:
        return self._by_isin.get((isin, exchange))

    def __contains__(self, key: tuple[str, str]) -> bool:
        return key in self._by_key

    def __len__(self) -> int:
        return len(self._by_key)
