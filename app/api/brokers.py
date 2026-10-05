"""Broker discovery and sessions: list brokers, build a login URL, connect, disconnect, read holdings."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from app.api.deps import get_registry, get_sessions
from app.brokers.registry import BrokerRegistry
from app.core.errors import CredentialsMissingError
from app.core.models import BrokerInfo, Holding, SessionInfo
from app.core.session_store import SessionStore

router = APIRouter()

Registry = Annotated[BrokerRegistry, Depends(get_registry)]
Sessions = Annotated[SessionStore, Depends(get_sessions)]


class CredentialsBody(BaseModel):
    credentials: dict[str, str] = Field(default_factory=dict)


class LoginUrlResponse(BaseModel):
    url: str | None


class HoldingsResponse(BaseModel):
    holdings: list[Holding]


@router.get("/brokers", tags=["brokers"], response_model=list[BrokerInfo])
async def list_brokers(registry: Registry) -> list[BrokerInfo]:
    return registry.describe()


@router.post("/brokers/{broker}/login-url", tags=["brokers"], response_model=LoginUrlResponse)
async def login_url(broker: str, body: CredentialsBody, registry: Registry) -> LoginUrlResponse:
    """The URL to send the user to. `null` for brokers that log in with direct credentials."""
    adapter = registry.get(broker)
    return LoginUrlResponse(url=adapter.login_url(body.credentials))


@router.post("/brokers/{broker}/sessions", tags=["sessions"], status_code=201, response_model=SessionInfo)
async def create_session(broker: str, body: CredentialsBody, registry: Registry,
                         sessions: Sessions) -> SessionInfo:
    adapter = registry.get(broker)
    missing = adapter.meta.missing_credentials(body.credentials)
    if missing:
        raise CredentialsMissingError(f"missing credentials: {', '.join(missing)}", details=missing)
    async with adapter.limiter.reads():
        session = await adapter.complete_login(body.credentials)
    sessions.put(session)
    return session.public()


@router.delete("/sessions/{session_id}", tags=["sessions"], status_code=204)
async def delete_session(session_id: str, sessions: Sessions) -> Response:
    sessions.get(session_id)  # 401 when unknown or expired
    sessions.drop(session_id)
    return Response(status_code=204)


@router.get("/sessions/{session_id}/holdings", tags=["sessions"], response_model=HoldingsResponse)
async def get_holdings(session_id: str, sessions: Sessions, registry: Registry) -> HoldingsResponse:
    session = sessions.get(session_id)
    adapter = registry.get(session.broker)
    async with adapter.limiter.reads():
        holdings = await adapter.get_holdings(session)
    return HoldingsResponse(holdings=holdings)
