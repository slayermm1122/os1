from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


SettingsCallback = Callable[[str], Awaitable[None] | None]
CloseCallback = Callable[[], Awaitable[None]]


class LiveSessionRegistry:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, tuple[SettingsCallback, CloseCallback]] = {}

    async def register(
        self,
        session_key: str,
        settings_callback: SettingsCallback,
        close_callback: CloseCallback,
    ) -> None:
        async with self._lock:
            self._sessions[session_key] = (settings_callback, close_callback)

    async def unregister(self, session_key: str) -> None:
        async with self._lock:
            self._sessions.pop(session_key, None)

    async def notify(self, kind: str) -> None:
        async with self._lock:
            callbacks = [callback for callback, _ in self._sessions.values()]
        for callback in callbacks:
            result = callback(kind)
            if result is not None:
                await result

    async def count(self) -> int:
        async with self._lock:
            return len(self._sessions)

    async def close_all(self) -> None:
        async with self._lock:
            callbacks = [callback for _, callback in self._sessions.values()]
            self._sessions.clear()
        await asyncio.gather(*(callback() for callback in callbacks), return_exceptions=True)
