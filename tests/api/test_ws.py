"""WebSocket stream tests, on Starlette's sync TestClient because httpx cannot speak WebSocket."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.core.models import RunStatus
from app.settings import Settings
from tests.api.fake_adapter import FakeAdapter
from tests.api.helpers import TEST_API_KEY, first_time_body


@pytest.fixture
def settings(instruments_path: str) -> Settings:
    """Longer poll budget than the HTTP tests: a held order must stay OPEN while the socket connects."""
    return Settings(_env_file=None, instruments_path=instruments_path, log_format="text", webhook_url=None,
                    poll_interval_s=0.005, poll_timeout_s=5.0, market_hours_warn=False, paper_latency_ms="0-0")


def open_session(sync_client: TestClient) -> str:
    response = sync_client.post("/brokers/fake/sessions", json={"credentials": {"api_key": TEST_API_KEY}})
    assert response.status_code == 201
    return response.json()["session_id"]


def read_until(ws: Any, event_type: str, limit: int = 200) -> list[dict[str, Any]]:
    """Collect JSON frames until one of `event_type` arrives (non-JSON frames like 'pong' are kept as text)."""
    frames: list[dict[str, Any]] = []
    for _ in range(limit):
        text = ws.receive_text()
        frame = json.loads(text) if text.startswith("{") else {"type": text}
        frames.append(frame)
        if frame["type"] == event_type:
            return frames
    raise AssertionError(f"no {event_type} frame within {limit} frames")


def test_unknown_run_closes_4404(sync_client: TestClient) -> None:
    with sync_client.websocket_connect("/ws/executions/nope") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4404


def test_terminal_run_gets_snapshot_then_normal_close(sync_client: TestClient) -> None:
    session_id = open_session(sync_client)
    run_id = sync_client.post("/executions", json=first_time_body(session_id, dry_run=True)).json()["run_id"]
    with sync_client.websocket_connect(f"/ws/executions/{run_id}") as ws:
        snapshot = json.loads(ws.receive_text())
        assert snapshot["type"] == "run.snapshot" and snapshot["report"]["status"] == "PLANNED"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1000


def test_live_run_streams_events_after_snapshot(sync_client: TestClient, fake: FakeAdapter) -> None:
    session_id = open_session(sync_client)
    fake.hold_fills = True  # orders stay OPEN until released
    accepted = sync_client.post("/executions", json=first_time_body(session_id))
    assert accepted.status_code == 202
    run_id = accepted.json()["run_id"]

    with sync_client.websocket_connect(f"/ws/executions/{run_id}") as ws:
        snapshot = json.loads(ws.receive_text())
        assert snapshot["type"] == "run.snapshot" and snapshot["report"]["status"] == "RUNNING"

        ws.send_text("ping")
        assert any(f["type"] == "pong" for f in read_until(ws, "pong"))

        fake.hold_fills = False
        frames = read_until(ws, "run.completed")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()

    assert closed.value.code == 1000
    types = [f["type"] for f in frames]
    assert types[-1] == "run.completed" and "order.updated" in types
    filled = [f for f in frames if f["type"] == "order.updated" and f["order"]["status"] == "FILLED"]
    assert {f["order"]["symbol"] for f in filled} == {"INFY", "TCS", "RELIANCE"}
    assert frames[-1]["report"]["status"] == "COMPLETED"
    assert all(f["run_id"] == run_id for f in frames if f["type"] != "pong")


def test_run_finishing_during_the_snapshot_send_still_delivers_completed(
        sync_client: TestClient, fake: FakeAdapter, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = open_session(sync_client)
    fake.hold_fills = True
    run_id = sync_client.post("/executions", json=first_time_body(session_id)).json()["run_id"]
    real_send_text = WebSocket.send_text

    async def slow_send_text(self: WebSocket, text: str) -> None:
        await real_send_text(self, text)
        if '"run.snapshot"' in text:  # the run finishes before the route regains control
            fake.hold_fills = False
            while app.state.runs.get(run_id).status is RunStatus.RUNNING:
                await asyncio.sleep(0.005)

    monkeypatch.setattr(WebSocket, "send_text", slow_send_text)
    with sync_client.websocket_connect(f"/ws/executions/{run_id}") as ws:
        frames = read_until(ws, "run.completed")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert frames[0]["type"] == "run.snapshot" and frames[0]["report"]["status"] == "RUNNING"
    assert frames[-1]["report"]["status"] == "COMPLETED" and closed.value.code == 1000
