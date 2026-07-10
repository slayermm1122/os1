from __future__ import annotations

import time


class SlidingWindowRateLimiter:
    def __init__(self, *, requests: int, window_seconds: int, max_clients: int = 10_000) -> None:
        self.requests = requests
        self.window_seconds = window_seconds
        self.max_clients = max(max_clients, 100)
        self._timestamps: dict[str, list[float]] = {}
        self._last_cleanup = 0.0

    def consume(self, client_id: str) -> bool:
        now = time.monotonic()
        window_start = now - self.window_seconds
        if now - self._last_cleanup >= self.window_seconds or len(self._timestamps) > self.max_clients:
            self._timestamps = {
                key: [value for value in values if value >= window_start]
                for key, values in self._timestamps.items()
                if any(value >= window_start for value in values)
            }
            self._last_cleanup = now
        if client_id not in self._timestamps and len(self._timestamps) >= self.max_clients:
            return False
        timestamps = [value for value in self._timestamps.get(client_id, []) if value >= window_start]
        if len(timestamps) >= self.requests:
            self._timestamps[client_id] = timestamps
            return False
        timestamps.append(now)
        self._timestamps[client_id] = timestamps
        return True
