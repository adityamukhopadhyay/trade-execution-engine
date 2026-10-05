"""FastAPI dependencies that read the components the lifespan stored on `app.state`."""
from __future__ import annotations

from collections import deque
from typing import Any

from fastapi.requests import HTTPConnection

from app.brokers.registry import BrokerRegistry
from app.core.session_store import SessionStore
from app.execution.service import ExecutionService
from app.notifications.websocket import WebSocketNotifier
from app.settings import Settings


def get_settings(conn: HTTPConnection) -> Settings:
    return conn.app.state.settings


def get_registry(conn: HTTPConnection) -> BrokerRegistry:
    return conn.app.state.registry


def get_sessions(conn: HTTPConnection) -> SessionStore:
    return conn.app.state.sessions


def get_service(conn: HTTPConnection) -> ExecutionService:
    return conn.app.state.service


def get_ws_notifier(conn: HTTPConnection) -> WebSocketNotifier:
    return conn.app.state.ws_notifier


def get_webhook_inbox(conn: HTTPConnection) -> deque[Any]:
    return conn.app.state.webhook_inbox
