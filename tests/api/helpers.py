"""Shared builders for the API tests: instrument rows, request bodies, a login helper, event factories."""
from __future__ import annotations

from typing import Any

import httpx

from app.core.models import (
    ExecutionEvent,
    ExecutionPlan,
    ExecutionReport,
    InstructionAction,
    OrderResult,
    OrderSide,
    OrderStatus,
    Phase,
    PlannedOrder,
    RunStatus,
    RunSummary,
    utcnow,
)

INSTRUMENT_ROWS: list[dict[str, Any]] = [
    {"symbol": "INFY", "exchange": "NSE", "isin": "INE009A01021", "name": "Infosys", "angel_token": "1594"},
    {"symbol": "TCS", "exchange": "NSE", "isin": "INE467B01029", "name": "TCS", "angel_token": "11536"},
    {"symbol": "RELIANCE", "exchange": "NSE", "isin": "INE002A01018", "name": "Reliance", "angel_token": "2885"},
    {"symbol": "HDFCBANK", "exchange": "NSE", "isin": "INE040A01034", "name": "HDFC Bank", "angel_token": "1333"},
    {"symbol": "SBIN", "exchange": "NSE", "isin": "INE062A01020", "name": "SBI", "angel_token": "3045"},
    {"symbol": "WIPRO", "exchange": "NSE", "isin": "INE075A01022", "name": "Wipro", "angel_token": "3787"},
    {"symbol": "REJECTME", "exchange": "NSE", "isin": "INE000000000", "name": "Always rejected", "angel_token": None},
]

TEST_API_KEY = "fake-test-key"


async def connect(client: httpx.AsyncClient, **knobs: str) -> str:
    """Open a session on the fake broker and return its id."""
    response = await client.post("/brokers/fake/sessions",
                                 json={"credentials": {"api_key": TEST_API_KEY, **knobs}})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def first_time_body(session_id: str, key: str | None = None, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": session_id,
        "portfolio": {"mode": "first_time", "lines": [
            {"symbol": "INFY", "exchange": "NSE", "quantity": 10},
            {"symbol": "TCS", "quantity": 5},
            {"symbol": "RELIANCE", "isin": "INE002A01018", "quantity": 8},
        ]},
    }
    if key:
        body["idempotency_key"] = key
    body.update(overrides)
    return body


def rebalance_body(session_id: str, key: str | None = None, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": session_id,
        "on_sell_shortfall": "halt",
        "portfolio": {"mode": "rebalance", "instructions": [
            {"action": "SELL", "symbol": "TCS", "quantity": 5},
            {"action": "REBALANCE", "symbol": "INFY", "quantity_delta": -4},
            {"action": "REBALANCE", "symbol": "RELIANCE", "quantity_delta": 2},
            {"action": "BUY", "symbol": "HDFCBANK", "quantity": 6},
        ]},
    }
    if key:
        body["idempotency_key"] = key
    body.update(overrides)
    return body


def make_report(run_id: str = "3f9c1a2b-test-run", status: RunStatus = RunStatus.COMPLETED,
                order_status: OrderStatus = OrderStatus.FILLED) -> ExecutionReport:
    """A one-order BUY report; enough shape for notifier and WebSocket tests."""
    run8 = "".join(ch for ch in run_id if ch.isalnum())[:8].ljust(8, "0")
    planned = PlannedOrder(tag=f"kp{run8}001", symbol="INFY", exchange="NSE", isin="INE009A01021",
                           side=OrderSide.BUY, quantity=10, seq=1, phase=Phase.BUY,
                           source_action=InstructionAction.BUY)
    plan = ExecutionPlan(run_id=run_id, session_id="sess", broker="fake", mode="first_time",
                         sells=[], buys=[planned], holdings_before=[])
    order = OrderResult.from_planned(planned).model_copy(update={
        "status": order_status, "filled_qty": 10 if order_status is OrderStatus.FILLED else 0,
    })
    summary = RunSummary(total=1, filled=1 if order_status is OrderStatus.FILLED else 0)
    return ExecutionReport(run_id=run_id, idempotency_key=None, broker="fake", mode="first_time", status=status,
                           dry_run=False, on_sell_shortfall="halt", plan=plan, orders=[order], summary=summary,
                           started_at=utcnow())


def make_event(event_type: str = "run.completed", run_id: str = "3f9c1a2b-test-run") -> ExecutionEvent:
    report = make_report(run_id)
    return ExecutionEvent(type=event_type, run_id=run_id, phase=Phase.BUY, order=report.orders[0], report=report)
