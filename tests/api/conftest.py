"""API test wiring: an app built from test settings with the fake broker registered, plus two clients."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from starlette.testclient import TestClient

from app.brokers.registry import REGISTRY
from app.main import create_app
from app.settings import Settings
from tests.api.fake_adapter import FakeAdapter
from tests.api.helpers import INSTRUMENT_ROWS, connect


@pytest.fixture
def instruments_path(tmp_path: Path) -> str:
    path = tmp_path / "instruments.json"
    path.write_text(json.dumps(INSTRUMENT_ROWS))
    return str(path)


@pytest.fixture
def settings(instruments_path: str) -> Settings:
    return Settings(
        _env_file=None, instruments_path=instruments_path, log_format="text", webhook_url=None,
        poll_interval_s=0.005, poll_timeout_s=0.3, retry_base_delay_s=0.001, retry_max_delay_s=0.002,
        ambiguous_lookup_attempts=3, ambiguous_lookup_interval_s=0.005, market_hours_warn=False,
        paper_latency_ms="0-0",
    )


@pytest.fixture
def app(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setitem(REGISTRY, "fake", FakeAdapter)
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


@pytest.fixture
def sync_client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as http:
        yield http


@pytest.fixture
def fake(app: FastAPI) -> FakeAdapter:
    """The live FakeAdapter instance; valid only while a client fixture holds the lifespan open."""
    return app.state.registry.get("fake")


@pytest.fixture
async def session_id(client: httpx.AsyncClient) -> str:
    return await connect(client)
