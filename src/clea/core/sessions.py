"""In-memory session management for the Milestone 1 core daemon."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from clea.core.protocol import new_id
from clea.providers.base import Message


class Session(BaseModel):
    """Conversation state for a single client session."""

    id: str = Field(default_factory=new_id)
    messages: list[Message] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    turn_count: int = 0
    working_summary: str = ""


class SessionManager:
    """Stores and retrieves active sessions for the daemon."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        """Create and store a new conversation session."""
        session = Session()
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        """Return an existing session by ID, or None if not found."""
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: str | None = None) -> Session:
        """Return the matching session or create a new one if omitted or unknown."""
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        session = Session(id=session_id) if session_id else Session()
        self._sessions[session.id] = session
        return session
