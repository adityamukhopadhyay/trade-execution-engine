"""In-memory broker sessions keyed by session id; tokens stay SecretStr and are never read here."""
from __future__ import annotations

from datetime import timedelta

from app.core.errors import SessionNotFoundError
from app.core.models import BrokerSession, utcnow


class SessionStore:
    def __init__(self, ttl_minutes: int) -> None:
        self._ttl = timedelta(minutes=ttl_minutes)
        self._sessions: dict[str, BrokerSession] = {}

    def put(self, session: BrokerSession) -> None:
        """Store it (expires_at capped at now + ttl so is_expired and the store agree) and sweep expired ones."""
        session.expires_at = min(session.expires_at, utcnow() + self._ttl)
        self._sessions = {k: s for k, s in self._sessions.items() if not s.is_expired}
        self._sessions[session.session_id] = session

    def get(self, session_id: str) -> BrokerSession:
        """The live session, or SessionNotFoundError (401) when it is missing or has expired."""
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError("session not found; connect a broker first")
        if session.is_expired:
            self.drop(session_id)
            raise SessionNotFoundError("session expired; connect the broker again")
        return session

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._sessions)
