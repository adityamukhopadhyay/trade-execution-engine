"""InMemoryRunRepository: save/get, newest-first listing, eviction, idempotency key binding."""
from __future__ import annotations

from app.execution.store import InMemoryRunRepository
from tests.fakes import first_time, make_report


def test_save_and_get(make_plan):
    runs = InMemoryRunRepository()
    report = make_report(make_plan(first_time(("INFY", 1))))
    runs.save(report)
    assert runs.get(report.run_id) is report
    assert runs.get("missing") is None


def test_list_newest_first_with_limit_and_session_filter(make_plan):
    runs = InMemoryRunRepository()
    reports = [make_report(make_plan(first_time(("INFY", 1)))) for _ in range(3)]
    for report in reports:
        runs.save(report)
    assert [r.run_id for r in runs.list(limit=10)] == [r.run_id for r in reversed(reports)]
    assert len(runs.list(limit=2)) == 2
    session_id = reports[0].plan.session_id
    assert len(runs.list(limit=10, session_id=session_id)) == 3
    assert runs.list(limit=10, session_id="other") == []


def test_resave_keeps_position(make_plan):
    runs = InMemoryRunRepository()
    first, second = (make_report(make_plan(first_time(("INFY", 1)))) for _ in range(2))
    runs.save(first)
    runs.save(second)
    runs.save(first)
    assert [r.run_id for r in runs.list(limit=10)] == [second.run_id, first.run_id]


def test_eviction_at_limit_drops_oldest_run_but_keeps_its_key(make_plan):
    runs = InMemoryRunRepository(limit=2)
    reports = [make_report(make_plan(first_time(("INFY", 1)))) for _ in range(3)]
    runs.save(reports[0])
    runs.bind_key("key-oldest-1", "hash", reports[0].run_id)
    runs.save(reports[1])
    runs.save(reports[2])
    assert runs.get(reports[0].run_id) is None
    assert runs.find_by_key("key-oldest-1") == (reports[0].run_id, "hash")
    assert [r.run_id for r in runs.list(limit=10)] == [reports[2].run_id, reports[1].run_id]


def test_key_bindings_are_bounded_independently():
    runs = InMemoryRunRepository(limit=2, key_limit=2)
    for i in range(3):
        runs.bind_key(f"key-{i}", "hash", f"run-{i}")
    assert runs.find_by_key("key-0") is None and runs.find_by_key("key-2") == ("run-2", "hash")


def test_find_by_key_then_bind():
    runs = InMemoryRunRepository()
    assert runs.find_by_key("demo-key-001") is None
    runs.bind_key("demo-key-001", "abc123", "run-1")
    assert runs.find_by_key("demo-key-001") == ("run-1", "abc123")
