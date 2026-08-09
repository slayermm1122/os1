from __future__ import annotations

import time

from .messages import Message


class KVConversationStore:
    """In-memory storage for the exact chronological model conversation."""

    def __init__(
        self,
        *,
        max_sessions: int,
        ttl_seconds: int,
        max_turns: int | None = None,
    ) -> None:
        # Retained as a no-op keyword for compatibility with older callers. OS1
        # now keeps every turn for the lifetime of the local session.
        del max_turns
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, list[Message]] = {}
        self._touched_at: dict[str, float] = {}

    def get_history(self, session_id: str) -> list[Message]:
        self._purge()
        history = self._sessions.get(session_id, [])
        if history:
            self._touched_at[session_id] = time.monotonic()
        return [message.copy() for message in history]

    def append_turn(self, session_id: str, model_user_text: str, assistant_text: str) -> None:
        self.append_exchange(session_id, model_user_text, assistant_text)

    def append_exchange(
        self,
        session_id: str,
        model_user_text: str,
        assistant_text: str | None,
    ) -> None:
        self._purge()
        history = self._sessions.setdefault(session_id, [])
        history.append({"role": "user", "content": model_user_text})
        if assistant_text:
            history.append({"role": "assistant", "content": assistant_text})
        self._touched_at[session_id] = time.monotonic()
        self._trim()

    def _purge(self) -> None:
        if self.ttl_seconds <= 0:
            return
        cutoff = time.monotonic() - self.ttl_seconds
        for session_id in [
            key for key, touched_at in self._touched_at.items() if touched_at < cutoff
        ]:
            self._sessions.pop(session_id, None)
            self._touched_at.pop(session_id, None)
    def _trim(self) -> None:
        if self.max_sessions <= 0:
            self._sessions.clear()
            self._touched_at.clear()
            return
        overflow = len(self._sessions) - self.max_sessions
        if overflow <= 0:
            return
        oldest = sorted(self._touched_at.items(), key=lambda item: item[1])[:overflow]
        for session_id, _ in oldest:
            self._sessions.pop(session_id, None)
            self._touched_at.pop(session_id, None)


# Backwards-compatible import while the rest of the application migrates to the
# product name used for its single, cache-aware model history.
SessionStore = KVConversationStore
