"""App factory: create_app() wires settings, components and routers; `app` is what uvicorn serves."""
from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import brokers, executions, health, mock_webhook, ws
from app.api.errors import install_error_handlers
from app.brokers.registry import BrokerRegistry
from app.core.instruments import InstrumentTable
from app.core.session_store import SessionStore
from app.execution.engine import ExecutionEngine
from app.execution.service import ExecutionService
from app.execution.store import InMemoryRunRepository
from app.logging import configure_logging, http_log_hooks
from app.notifications.base import Notifier
from app.notifications.console import ConsoleNotifier
from app.notifications.fanout import FanoutNotifier
from app.notifications.webhook import WebhookNotifier
from app.notifications.websocket import WebSocketNotifier
from app.settings import Settings

log = logging.getLogger("app.main")

STATIC_DIR = Path(__file__).parent / "static"
WEBHOOK_INBOX_LIMIT = 50
OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness."},
    {"name": "brokers", "description": "Which brokers exist and what each needs to log in."},
    {"name": "sessions", "description": "Connect to a broker, read holdings, disconnect."},
    {"name": "executions", "description": "Submit a portfolio and follow the run."},
    {"name": "notifications", "description": "Live event stream over WebSocket."},
    {"name": "mock", "description": "Demo webhook receiver (the app posts to itself)."},
    {"name": "ui", "description": "The single-page frontend."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    state = app.state
    state.started_at = time.monotonic()
    state.instruments = InstrumentTable.load(settings.instruments_path)
    state.http = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http_timeout_s, connect=5.0),
        headers={"User-Agent": "trade-execution-engine/" + health.app_version()},
        event_hooks=http_log_hooks(),
    )
    state.registry = BrokerRegistry(state.http, state.instruments, settings.broker_configs())
    state.sessions = SessionStore(settings.session_ttl_min)
    state.runs = InMemoryRunRepository(settings.run_store_limit)
    state.ws_notifier = WebSocketNotifier()
    sinks: list[Notifier] = [ConsoleNotifier()]
    if settings.webhook_url:
        sinks.append(WebhookNotifier(state.http, settings.webhook_url, timeout_s=settings.webhook_timeout_s))
    sinks.append(state.ws_notifier)
    state.notifier = FanoutNotifier(sinks)
    engine_config = settings.engine_config()
    state.engine = ExecutionEngine(state.runs, state.notifier, engine_config)
    state.service = ExecutionService(state.sessions, state.registry, state.runs, state.engine,
                                     state.instruments, engine_config)
    state.webhook_inbox = deque(maxlen=WEBHOOK_INBOX_LIMIT)
    log.info("app.started env=%s instruments=%d brokers=%s webhook=%s",
             settings.app_env, len(state.instruments), sorted(a.name for a in state.registry.describe()),
             "on" if settings.webhook_url else "off")
    try:
        yield
    finally:
        await state.service.aclose()
        await state.notifier.close()
        await state.http.aclose()
        log.info("app.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_format)
    app = FastAPI(
        title="Portfolio Trade Execution Engine", version=health.app_version(),
        description="Upload a target portfolio, connect a broker, execute, watch the result.",
        lifespan=lifespan, openapi_tags=OPENAPI_TAGS,
    )
    app.state.settings = settings
    install_error_handlers(app)
    for router in (health.router, brokers.router, executions.router, ws.router, mock_webhook.router):
        app.include_router(router)
    mount_ui(app)
    return app


def mount_ui(app: FastAPI) -> None:
    """`/` serves index.html; `/static/*` serves the rest of the frontend (samples, js, css)."""
    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

    @app.get("/", tags=["ui"], summary="The single-page UI")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")


app = create_app()
