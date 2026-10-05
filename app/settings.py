"""Configuration from environment variables (and an optional .env file)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.execution.engine import EngineConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    instruments_path: str = "data/instruments.json"

    session_ttl_min: int = 480
    http_timeout_s: float = 10.0
    max_inflight_per_broker: int = 4

    place_max_attempts: int = 3
    retry_base_delay_s: float = 0.25
    retry_max_delay_s: float = 5.0
    poll_interval_s: float = 1.0
    poll_timeout_s: float = 30.0
    ambiguous_lookup_attempts: int = 5
    ambiguous_lookup_interval_s: float = 2.0
    market_hours_warn: bool = True

    webhook_url: str | None = "http://localhost:8000/mock/webhook"  # blank disables the webhook sink
    webhook_timeout_s: float = 5.0
    run_store_limit: int = 200

    paper_seed_holdings: str = ""
    paper_reject_rate: float = 0.0
    paper_latency_ms: str = "20-120"       # "lo-hi" milliseconds
    paper_rate_limit_every_n: int = 0
    paper_ambiguous_rate: float = 0.0
    paper_ambiguous_placed: bool = True
    paper_partial_fill_rate: float = 0.0
    paper_fill_after_polls: int = 1
    paper_seed: int = 42

    zerodha_base_url: str | None = None
    fyers_base_url: str | None = None
    angelone_base_url: str | None = None
    upstox_base_url: str | None = None
    upstox_order_base_url: str | None = None
    groww_base_url: str | None = None

    def engine_config(self) -> EngineConfig:
        return EngineConfig(
            place_max_attempts=self.place_max_attempts,
            retry_base_delay_s=self.retry_base_delay_s,
            retry_max_delay_s=self.retry_max_delay_s,
            poll_interval_s=self.poll_interval_s,
            poll_timeout_s=self.poll_timeout_s,
            ambiguous_lookup_attempts=self.ambiguous_lookup_attempts,
            ambiguous_lookup_interval_s=self.ambiguous_lookup_interval_s,
            market_hours_warn=self.market_hours_warn,
        )

    def paper_faults(self) -> dict[str, Any]:
        """The PAPER_* fault knobs in the shape `PaperFaults(**faults)` expects."""
        return {
            "reject_rate": self.paper_reject_rate,
            "latency_ms": parse_range(self.paper_latency_ms),
            "rate_limit_every_n": self.paper_rate_limit_every_n,
            "ambiguous_rate": self.paper_ambiguous_rate,
            "ambiguous_placed": self.paper_ambiguous_placed,
            "partial_fill_rate": self.paper_partial_fill_rate,
            "fill_after_polls": self.paper_fill_after_polls,
            "seed": self.paper_seed,
        }

    def broker_configs(self) -> dict[str, dict[str, Any]]:
        """Per-broker config dicts for BrokerRegistry; a real broker gets base_url only when its env
        override is set."""
        inflight = self.max_inflight_per_broker
        configs: dict[str, dict[str, Any]] = {
            "paper": {"seed_holdings": self.paper_seed_holdings, "faults": self.paper_faults(),
                      "max_inflight": inflight},
        }
        base_urls = {
            "zerodha": self.zerodha_base_url,
            "fyers": self.fyers_base_url,
            "angelone": self.angelone_base_url,
            "upstox": self.upstox_base_url,
            "groww": self.groww_base_url,
        }
        for name, url in base_urls.items():
            configs[name] = {"max_inflight": inflight}
            if url:
                configs[name]["base_url"] = url
        if self.upstox_order_base_url:
            configs["upstox"]["order_base_url"] = self.upstox_order_base_url
        return configs


def parse_range(text: str) -> tuple[int, int]:
    """'20-120' -> (20, 120); a single number means a fixed value, e.g. '50' -> (50, 50)."""
    lo, _, hi = text.strip().partition("-")
    low = int(lo or 0)
    return (low, int(hi) if hi else low)
