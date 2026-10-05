"""ExecutionService.submit(): idempotency, sessions, validation, dry run, waiting and background tasks."""
from __future__ import annotations

import asyncio

import pytest

from app.brokers.base import AuthError
from app.core.errors import (
    IdempotencyConflictError,
    PortfolioInvalidError,
    RunNotFoundError,
    SessionNotFoundError,
)
from app.core.models import ExecuteRequest, OrderStatus, RunStatus
from app.core.session_store import SessionStore
from app.execution.engine import ExecutionEngine
from app.execution.service import ExecutionService
from app.execution.store import InMemoryRunRepository
from tests.fakes import ScriptedRegistry, first_time, holding, rebalance, sell

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sessions(session) -> SessionStore:
    store = SessionStore(ttl_minutes=480)
    store.put(session)
    return store


@pytest.fixture
def service(sessions, adapter, runs, engine, instruments, fast_config) -> ExecutionService:
    return ExecutionService(sessions, ScriptedRegistry(adapter), runs, engine, instruments, fast_config)


def request(session, portfolio=None, **fields) -> ExecuteRequest:
    return ExecuteRequest(session_id=session.session_id, portfolio=portfolio or first_time(("INFY", 1)),
                          **fields)


async def test_wait_returns_a_terminal_report(service, session):
    report, replayed = await service.submit(request(session), wait=True)
    assert replayed is False and report.status is RunStatus.COMPLETED
    assert service.get(report.run_id) is report


async def test_replay_returns_the_same_run(service, session, adapter):
    first, _ = await service.submit(request(session, idempotency_key="demo-first-001"), wait=True)
    again, replayed = await service.submit(request(session, idempotency_key="demo-first-001"), wait=True)
    assert replayed is True and again.run_id == first.run_id
    assert adapter.count("place_order") == 1


async def test_concurrent_submits_with_one_key_run_once(service, session, adapter, monkeypatch):
    real_get_holdings = adapter.get_holdings

    async def slow_get_holdings(session):
        await asyncio.sleep(0.01)
        return await real_get_holdings(session)

    monkeypatch.setattr(adapter, "get_holdings", slow_get_holdings)
    requests = [request(session, idempotency_key="demo-first-001") for _ in range(2)]
    results = await asyncio.gather(*(service.submit(req, wait=True) for req in requests))
    assert len({report.run_id for report, _ in results}) == 1
    assert sorted(replayed for _, replayed in results) == [False, True]
    assert adapter.count("place_order") == 1


async def test_replay_of_an_evicted_run_is_refused(sessions, adapter, notifier, instruments, fast_config, session):
    runs = InMemoryRunRepository(limit=1)
    engine = ExecutionEngine(runs, notifier, fast_config)
    service = ExecutionService(sessions, ScriptedRegistry(adapter), runs, engine, instruments, fast_config)
    await service.submit(request(session, idempotency_key="demo-first-001"), wait=True)
    await service.submit(request(session, first_time(("TCS", 1))), wait=True)
    with pytest.raises(IdempotencyConflictError, match="no longer stored"):
        await service.submit(request(session, idempotency_key="demo-first-001"), wait=True)
    assert adapter.count("place_order") == 2


async def test_same_key_different_payload_conflicts(service, session):
    await service.submit(request(session, idempotency_key="demo-first-001"), wait=True)
    with pytest.raises(IdempotencyConflictError):
        await service.submit(request(session, first_time(("TCS", 2)), idempotency_key="demo-first-001"))


async def test_missing_or_expired_session(service, session, sessions, adapter):
    with pytest.raises(SessionNotFoundError):
        await service.submit(ExecuteRequest(session_id="nope", portfolio=first_time(("INFY", 1))))
    sessions.drop(session.session_id)
    with pytest.raises(SessionNotFoundError):
        await service.submit(request(session))
    assert adapter.calls == []


async def test_validation_failure_places_nothing(service, session, adapter, runs):
    bad = rebalance(sell("INFY", 99), sell("NOPE", 1))
    with pytest.raises(PortfolioInvalidError) as excinfo:
        await service.submit(request(session, bad))
    assert [issue["symbol"] for issue in excinfo.value.details] == ["NOPE", "INFY"]
    assert adapter.count("place_order") == 0 and runs.list(limit=10) == []


async def test_holdings_auth_error_propagates(service, session, adapter):
    adapter.holdings_queue.append(AuthError("token expired"))
    with pytest.raises(AuthError):
        await service.submit(request(session))
    assert adapter.count("place_order") == 0


async def test_dry_run_plans_without_placing(service, session, adapter):
    report, replayed = await service.submit(request(session, dry_run=True, idempotency_key="demo-dry-0001"))
    assert replayed is False and report.status is RunStatus.PLANNED and report.dry_run is True
    assert all(order.status is OrderStatus.PENDING for order in report.orders)
    assert adapter.count("place_order") == 0 and service.get(report.run_id) is report
    again, replayed = await service.submit(request(session, dry_run=True, idempotency_key="demo-dry-0001"))
    assert replayed is True and again.run_id == report.run_id


async def test_background_task_is_tracked_and_closed(service, session):
    report, _ = await service.submit(request(session), wait=False)
    assert report.status is RunStatus.RUNNING and len(service._tasks) == 1
    await service.aclose()
    assert report.status is RunStatus.COMPLETED and service._tasks == set()


async def test_holdings_snapshot_feeds_validation_and_plan(service, session, adapter):
    adapter.holdings = [holding("INFY", 10)]
    report, _ = await service.submit(request(session, rebalance(sell("INFY", 4))), wait=True)
    assert report.plan.holdings_before == [holding("INFY", 10)] and report.mode == "rebalance"
    assert report.status is RunStatus.COMPLETED


async def test_get_and_list(service, session):
    with pytest.raises(RunNotFoundError):
        service.get("missing")
    first, _ = await service.submit(request(session), wait=True)
    second, _ = await service.submit(request(session, first_time(("TCS", 1))), wait=True)
    assert [r.run_id for r in service.list(limit=10)] == [second.run_id, first.run_id]
    assert service.list(limit=10, session_id="other") == []
