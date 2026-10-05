"""Notifier sinks: fanout isolation, webhook retry/swallow, console line, websocket broadcast + pruning."""
from __future__ import annotations

import logging

import httpx
import pytest
import respx

from app.core.models import ExecutionEvent
from app.notifications.base import Notifier
from app.notifications.console import ConsoleNotifier
from app.notifications.fanout import FanoutNotifier
from app.notifications.webhook import WebhookNotifier
from app.notifications.websocket import WebSocketNotifier
from tests.api.helpers import make_event

WEBHOOK_URL = "http://hooks.example/receive"


class RecordingSink(Notifier):
    def __init__(self) -> None:
        self.events: list[ExecutionEvent] = []

    async def notify(self, event: ExecutionEvent) -> None:
        self.events.append(event)


class RaisingSink(Notifier):
    async def notify(self, event: ExecutionEvent) -> None:
        raise RuntimeError("sink is broken")


class FakeSocket:
    """Looks enough like a Starlette WebSocket for the notifier: send_text / close."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail
        self.closed = False

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("socket closed by peer")
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed = True


async def test_fanout_delivers_to_every_sink_and_isolates_failures(caplog: pytest.LogCaptureFixture) -> None:
    first, second = RecordingSink(), RecordingSink()
    fanout = FanoutNotifier([first, RaisingSink(), second])
    event = make_event()
    with caplog.at_level(logging.WARNING, logger="app.notify.fanout"):
        await fanout.notify(event)  # must not raise
    assert first.events == [event] and second.events == [event]
    assert any("notify.failed sink=RaisingSink" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_webhook_posts_event_json() -> None:
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as http:
        await WebhookNotifier(http, WEBHOOK_URL, timeout_s=1).notify(make_event())
    assert route.call_count == 1
    body = route.calls[0].request.content
    assert b'"type":"run.completed"' in body and b'"run_id":"3f9c1a2b-test-run"' in body


@respx.mock
async def test_webhook_retries_once_on_500_then_succeeds() -> None:
    route = respx.post(WEBHOOK_URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
    async with httpx.AsyncClient() as http:
        await WebhookNotifier(http, WEBHOOK_URL, timeout_s=1).notify(make_event())
    assert route.call_count == 2


@respx.mock
async def test_webhook_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    always_down = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        notifier = WebhookNotifier(http, WEBHOOK_URL, timeout_s=1)
        with caplog.at_level(logging.ERROR, logger="app.notify.webhook"):
            await notifier.notify(make_event())
        assert always_down.call_count == 2
        assert any("notify.failed sink=webhook" in r.getMessage() for r in caplog.records)

        always_down.mock(side_effect=httpx.ConnectError("refused"))
        await notifier.notify(make_event())  # transport error: still swallowed
    assert always_down.call_count == 4


@respx.mock
async def test_webhook_only_posts_subscribed_events() -> None:
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as http:
        await WebhookNotifier(http, WEBHOOK_URL, timeout_s=1).notify(make_event("run.started"))
        await WebhookNotifier(http, WEBHOOK_URL, timeout_s=1, events={"run.started"}).notify(make_event("run.started"))
    assert route.call_count == 1


async def test_console_logs_one_line_with_run_id_and_counts(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.notify.console"):
        await ConsoleNotifier().notify(make_event())
    lines = [r for r in caplog.records if r.name == "app.notify.console"]
    assert len(lines) == 1
    message = lines[0].getMessage()
    assert "run_id=3f9c1a2b-test-run" in message and "filled=1" in message and "type=run.completed" in message
    assert lines[0].run_id == "3f9c1a2b-test-run"  # type: ignore[attr-defined]


async def test_websocket_broadcasts_to_that_run_only_and_prunes_dead_sockets() -> None:
    notifier = WebSocketNotifier()
    healthy, dead, other_run = FakeSocket(), FakeSocket(fail=True), FakeSocket()
    notifier.register("run-a", healthy)  # type: ignore[arg-type]
    notifier.register("run-a", dead)  # type: ignore[arg-type]
    notifier.register("run-b", other_run)  # type: ignore[arg-type]

    await notifier.notify(make_event(run_id="run-a"))

    assert len(healthy.sent) == 1 and '"type":"run.completed"' in healthy.sent[0]
    assert healthy.closed and not other_run.closed
    assert other_run.sent == []
    assert notifier.watchers("run-a") == 1 and notifier.watchers("run-b") == 1

    notifier.unregister("run-a", healthy)  # type: ignore[arg-type]
    assert notifier.watchers("run-a") == 0
    await notifier.close()
    assert other_run.closed and notifier.watchers("run-b") == 0
