"""SessionStore: put/get, TTL expiry evicts, drop, secrets stay SecretStr."""
from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import SecretStr

from app.core.errors import SessionNotFoundError
from app.core.models import BrokerSession, utcnow
from app.core.session_store import SessionStore


def make_session(session_id: str = "s1", **overrides) -> BrokerSession:
    fields = dict(session_id=session_id, broker="scripted", access_token=SecretStr("tok-123"),
                  expires_at=utcnow() + timedelta(hours=8))
    return BrokerSession(**{**fields, **overrides})


def test_put_and_get_returns_the_session_with_secret_intact():
    store = SessionStore(ttl_minutes=480)
    store.put(make_session())
    session = store.get("s1")
    assert isinstance(session.access_token, SecretStr)
    assert session.access_token.get_secret_value() == "tok-123"
    assert "tok-123" not in repr(session)


def test_missing_raises():
    with pytest.raises(SessionNotFoundError):
        SessionStore(ttl_minutes=480).get("nope")


def test_ttl_caps_broker_expiry_and_expired_is_evicted():
    store = SessionStore(ttl_minutes=0)
    store.put(make_session())
    assert len(store) == 1
    with pytest.raises(SessionNotFoundError, match="expired"):
        store.get("s1")
    assert len(store) == 0


def test_put_sweeps_expired_sessions():
    store = SessionStore(ttl_minutes=0)
    store.put(make_session("s1"))
    store.put(make_session("s2"))
    assert len(store) == 1


def test_broker_expiry_in_the_past_is_expired():
    store = SessionStore(ttl_minutes=480)
    store.put(make_session(expires_at=utcnow() - timedelta(seconds=1)))
    with pytest.raises(SessionNotFoundError):
        store.get("s1")


def test_drop_is_idempotent():
    store = SessionStore(ttl_minutes=480)
    store.put(make_session())
    store.drop("s1")
    store.drop("s1")
    with pytest.raises(SessionNotFoundError):
        store.get("s1")
