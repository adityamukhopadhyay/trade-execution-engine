"""Shared fixtures: instrument table, fast EngineConfig, scripted session and adapter, engine."""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.instruments import Instrument, InstrumentTable
from app.core.models import BrokerSession, ExecutionPlan, FirstTimePayload, Holding, RebalancePayload, utcnow
from app.execution.engine import EngineConfig, ExecutionEngine
from app.execution.planner import build_plan
from app.execution.store import InMemoryRunRepository
from tests.fakes import RecordingNotifier, ScriptedAdapter

ISINS = {"INFY": "INE009A01021", "TCS": "INE467B01029", "RELIANCE": "INE002A01018",
         "HDFCBANK": "INE040A01034", "SBIN": "INE062A01020", "WIPRO": "INE075A01022",
         "REJECTME": "INE000000001"}


@pytest.fixture
def instruments() -> InstrumentTable:
    return InstrumentTable(Instrument(symbol=s, exchange="NSE", isin=isin, name=s, angel_token="1")
                           for s, isin in ISINS.items())


@pytest.fixture
def fast_config() -> EngineConfig:
    return EngineConfig(place_max_attempts=3, retry_base_delay_s=0.001, retry_max_delay_s=0.002,
                        poll_interval_s=0.005, poll_timeout_s=0.2, ambiguous_lookup_attempts=3,
                        ambiguous_lookup_interval_s=0.005, market_hours_warn=False)


@pytest.fixture
def session() -> BrokerSession:
    return BrokerSession(session_id="sess-" + uuid4().hex[:8], broker="scripted", user_id="tester",
                         access_token=SecretStr("not-a-real-token"), expires_at=utcnow() + timedelta(hours=8))


@pytest.fixture
def adapter() -> ScriptedAdapter:
    return ScriptedAdapter()


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def runs() -> InMemoryRunRepository:
    return InMemoryRunRepository(limit=50)


@pytest.fixture
def engine(runs: InMemoryRunRepository, notifier: RecordingNotifier,
           fast_config: EngineConfig) -> ExecutionEngine:
    return ExecutionEngine(runs, notifier, fast_config)


PlanFactory = Callable[..., ExecutionPlan]


@pytest.fixture
def make_plan(session: BrokerSession, instruments: InstrumentTable) -> PlanFactory:
    """make_plan(portfolio, holdings=[]) -> ExecutionPlan with a fresh hex run_id and no market warning."""
    def _make(portfolio: FirstTimePayload | RebalancePayload,
              holdings: list[Holding] | None = None) -> ExecutionPlan:
        return build_plan(uuid4().hex, session, portfolio, holdings or [], instruments,
                          market_hours_warn=False)
    return _make
