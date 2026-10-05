"""HTTP API end to end against the FakeAdapter: brokers, sessions, holdings, executions, errors, webhook."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request

from app.api.errors import handle_broker_error
from app.brokers.base import RateLimitError
from tests.api.fake_adapter import FakeAdapter
from tests.api.helpers import TEST_API_KEY, connect, first_time_body, rebalance_body

STATIC_INDEX = Path(__file__).resolve().parents[2] / "app" / "static" / "index.html"


async def test_health(client: httpx.AsyncClient) -> None:
    body = (await client.get("/health")).json()
    assert body["status"] == "ok" and body["uptime_s"] >= 0
    assert {"paper", "fake"} <= set(body["brokers"])


async def test_brokers_list(client: httpx.AsyncClient) -> None:
    brokers = (await client.get("/brokers")).json()
    by_name = {b["name"]: b for b in brokers}
    assert {"paper", "zerodha", "fyers", "angelone", "upstox", "groww", "fake"} <= set(by_name)
    assert by_name["fake"]["required_credentials"] == ["api_key"]
    assert all(isinstance(b["live_tested"], bool) for b in brokers)


async def test_login_url(client: httpx.AsyncClient) -> None:
    with_key = await client.post("/brokers/fake/login-url", json={"credentials": {"api_key": "k1"}})
    assert with_key.json()["url"] == "https://fake.example/login?api_key=k1"
    paper = await client.post("/brokers/paper/login-url", json={"credentials": {}})
    assert paper.status_code == 200 and paper.json()["url"] is None
    unknown = await client.post("/brokers/nope/login-url", json={"credentials": {}})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "unknown_broker"


async def test_create_session_returns_session_info_without_token(client: httpx.AsyncClient) -> None:
    response = await client.post("/brokers/fake/sessions", json={"credentials": {"api_key": TEST_API_KEY}})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"session_id", "broker", "user_id", "expires_at"} and body["broker"] == "fake"
    assert "token" not in response.text


async def test_create_session_missing_credentials_422(client: httpx.AsyncClient) -> None:
    response = await client.post("/brokers/fake/sessions", json={"credentials": {}})
    assert response.status_code == 422
    assert response.json()["error"] == {"code": "credentials_missing", "details": ["api_key"],
                                        "message": "missing credentials: api_key"}


async def test_create_session_bad_credentials_401(client: httpx.AsyncClient) -> None:
    response = await client.post("/brokers/fake/sessions", json={"credentials": {"api_key": "bad"}})
    assert response.status_code == 401 and response.json()["error"]["code"] == "broker_auth"


async def test_holdings_and_delete(client: httpx.AsyncClient) -> None:
    session_id = await connect(client, seed_holdings="TCS:5,INFY:10")
    holdings = (await client.get(f"/sessions/{session_id}/holdings")).json()["holdings"]
    assert [(h["symbol"], h["quantity"]) for h in holdings] == [("INFY", 10), ("TCS", 5)]
    assert (await client.delete(f"/sessions/{session_id}")).status_code == 204
    gone = await client.get(f"/sessions/{session_id}/holdings")
    assert gone.status_code == 401 and gone.json()["error"]["code"] == "session_not_found"


async def test_dry_run_plans_without_placing(client: httpx.AsyncClient, session_id: str, fake: FakeAdapter) -> None:
    response = await client.post("/executions", json=first_time_body(session_id, dry_run=True))
    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "PLANNED" and report["dry_run"] is True
    assert [o["status"] for o in report["orders"]] == ["PENDING"] * 3
    assert fake.place_calls == 0


async def test_first_time_with_wait_completes(client: httpx.AsyncClient, session_id: str) -> None:
    response = await client.post("/executions?wait=true", json=first_time_body(session_id))
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "COMPLETED" and report["summary"]["filled"] == 3
    assert all(o["status"] == "FILLED" and o["filled_qty"] == o["quantity"] for o in report["orders"])
    assert all(o["side"] == "BUY" and o["phase"] == "BUY" for o in report["orders"])
    assert report["reconciliation"]["status"] == "MATCH"
    held = (await client.get(f"/sessions/{session_id}/holdings")).json()["holdings"]
    assert {h["symbol"] for h in held} == {"INFY", "TCS", "RELIANCE"}


async def test_async_submit_then_poll(client: httpx.AsyncClient, session_id: str) -> None:
    accepted = await client.post("/executions", json=first_time_body(session_id))
    assert accepted.status_code == 202 and accepted.json()["status"] == "RUNNING"
    run_id = accepted.json()["run_id"]
    for _ in range(200):
        report = (await client.get(f"/executions/{run_id}")).json()
        if report["status"] != "RUNNING":
            break
        await asyncio.sleep(0.01)
    assert report["status"] == "COMPLETED"


async def test_first_time_with_existing_holdings(client: httpx.AsyncClient) -> None:
    session_id = await connect(client, seed_holdings="SBIN:3")
    rejected = await client.post("/executions?wait=true", json=first_time_body(session_id))
    assert rejected.status_code == 422
    error = rejected.json()["error"]
    assert error["code"] == "portfolio_invalid" and error["details"]
    allowed = await client.post("/executions?wait=true",
                                json=first_time_body(session_id, allow_existing_holdings=True))
    assert allowed.status_code == 200 and allowed.json()["status"] == "COMPLETED"


async def test_rebalance_sells_before_buys(client: httpx.AsyncClient) -> None:
    session_id = await connect(client, seed_holdings="INFY:10,TCS:5,RELIANCE:8")
    response = await client.post("/executions?wait=true", json=rebalance_body(session_id))
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "COMPLETED" and report["summary"]["total"] == 4
    sells = [o for o in report["orders"] if o["side"] == "SELL"]
    buys = [o for o in report["orders"] if o["side"] == "BUY"]
    assert [(o["symbol"], o["quantity"]) for o in sells] == [("TCS", 5), ("INFY", 4)]
    assert [(o["symbol"], o["quantity"]) for o in buys] == [("RELIANCE", 2), ("HDFCBANK", 6)]
    assert max(o["seq"] for o in sells) < min(o["seq"] for o in buys)
    assert max(o["submitted_at"] for o in sells) <= min(o["submitted_at"] for o in buys)
    holdings = (await client.get(f"/sessions/{session_id}/holdings")).json()["holdings"]
    held = {h["symbol"]: h["quantity"] for h in holdings}
    assert held == {"INFY": 6, "RELIANCE": 10, "HDFCBANK": 6}


async def test_oversell_is_422_with_details(client: httpx.AsyncClient, fake: FakeAdapter) -> None:
    session_id = await connect(client, seed_holdings="TCS:2")
    instructions = [{"action": "SELL", "symbol": "TCS", "quantity": 50}]
    response = await client.post("/executions", json={"session_id": session_id,
                                                      "portfolio": {"mode": "rebalance", "instructions": instructions}})
    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert any(d["symbol"] == "TCS" for d in details) and fake.place_calls == 0


async def test_idempotent_replay_and_conflict(client: httpx.AsyncClient, session_id: str, fake: FakeAdapter) -> None:
    key = "demo-first-001"
    first = await client.post("/executions?wait=true", json=first_time_body(session_id, key))
    assert first.status_code == 200 and "idempotent-replay" not in first.headers
    placed = fake.place_calls

    replay = await client.post("/executions?wait=true", json=first_time_body(session_id, key))
    assert replay.status_code == 200 and replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["run_id"] == first.json()["run_id"] and fake.place_calls == placed

    conflict = await client.post("/executions", json=first_time_body(session_id, key, dry_run=True))
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"


async def test_execute_with_unknown_session_401(client: httpx.AsyncClient) -> None:
    response = await client.post("/executions", json=first_time_body("not-a-session"))
    assert response.status_code == 401 and response.json()["error"]["code"] == "session_not_found"


async def test_bad_request_shape_422_request_invalid(client: httpx.AsyncClient, session_id: str) -> None:
    response = await client.post("/executions", json={"session_id": session_id, "portfolio": {"mode": "first_time"}})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "request_invalid" and error["details"][0]["loc"][:2] == ["body", "portfolio"]


async def test_list_and_get_runs(client: httpx.AsyncClient, session_id: str) -> None:
    other = await connect(client)
    run_a = (await client.post("/executions?wait=true", json=first_time_body(session_id))).json()["run_id"]
    run_b = (await client.post("/executions?wait=true", json=first_time_body(other))).json()["run_id"]
    listed = [r["run_id"] for r in (await client.get("/executions")).json()]
    assert listed[:2] == [run_b, run_a]
    mine = (await client.get("/executions", params={"session_id": session_id})).json()
    assert [r["run_id"] for r in mine] == [run_a]
    one = await client.get(f"/executions/{run_a}")
    assert one.json()["status"] == "COMPLETED" and "session_id" not in one.text
    missing = await client.get("/executions/does-not-exist")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "run_not_found"


async def test_rate_limit_error_maps_to_429_with_retry_after() -> None:
    request = Request({"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b""})
    response = await handle_broker_error(request, RateLimitError("slow down", retry_after=2.2))
    assert response.status_code == 429 and response.headers["Retry-After"] == "3"
    assert b'"code":"broker_rate_limited"' in response.body


async def test_unhandled_exception_is_a_json_500(app: FastAPI, client: httpx.AsyncClient, fake: FakeAdapter,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = await connect(client)

    async def boom(session):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(fake, "get_holdings", boom)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as raw:
        response = await raw.get(f"/sessions/{session_id}/holdings")
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal"
    assert "secret detail" not in response.text


async def test_mock_webhook_roundtrip(client: httpx.AsyncClient) -> None:
    body = {"type": "run.completed", "run_id": "r1", "report": {"status": "COMPLETED", "summary": {"filled": 2}}}
    assert (await client.post("/mock/webhook", json=body)).status_code == 204
    assert (await client.post("/mock/webhook", json={"anything": True})).status_code == 204
    deliveries = (await client.get("/mock/webhook")).json()["deliveries"]
    assert deliveries[0] == {"anything": True} and deliveries[1] == body


async def test_index_page(client: httpx.AsyncClient) -> None:
    if not STATIC_INDEX.exists():
        pytest.skip("frontend not built yet")
    response = await client.get("/")
    assert response.status_code == 200 and 'id="portfolio"' in response.text
