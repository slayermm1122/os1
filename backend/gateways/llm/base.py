from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

from ...core.messages import Message


@dataclass(frozen=True)
class LLMRequest:
    messages: list[Message]
    api_key: str | None = None
    cache_key: str | None = None
    purpose: str = "answer"


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd_ticks: int | None = None

    @property
    def cache_status(self) -> str | None:
        if self.cached_tokens is None or self.prompt_tokens is None:
            return None
        if self.cached_tokens <= 0:
            return "miss"
        if self.cached_tokens >= self.prompt_tokens:
            return "full"
        return "partial"


@dataclass(frozen=True)
class LLMStreamEvent:
    kind: Literal["delta", "complete"]
    text: str = ""
    usage: LLMUsage | None = None
    finish_reason: str | None = None
    request_id: str | None = None
    system_fingerprint: str | None = None
    service_tier: str | None = None


class AIGateway(Protocol):
    provider: str
    model: str
    reasoning_effort: str

    def request_snapshot(self, request: LLMRequest) -> dict[str, object]: ...

    def stream_text(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]: ...

LLMGateway = AIGateway
