from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
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
    response_model: str | None = None
    response_reasoning_setting: str | None = None


class AIGateway(Protocol):
    provider: str
    model: str
    reasoning_setting: str | None
    api_key_configured: bool

    def request_snapshot(self, request: LLMRequest) -> dict[str, object]: ...

    def stream_text(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]: ...


class AIAdapter:
    """Request-scoped router for provider-specific model gateways."""

    def __init__(
        self,
        providers: Iterable[AIGateway],
        *,
        default_provider: str,
    ) -> None:
        self._providers: dict[str, AIGateway] = {}
        for gateway in providers:
            provider = gateway.provider.strip().lower()
            if not provider:
                raise ValueError("AI providers must have a non-empty provider name.")
            if provider in self._providers:
                raise ValueError(f"Duplicate AI provider: {provider}")
            self._providers[provider] = gateway
        self.default_provider = default_provider.strip().lower()
        if self.default_provider not in self._providers:
            raise ValueError(f"Default AI provider is not registered: {self.default_provider}")

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def select(self, provider: str) -> None:
        selected = provider.strip().lower()
        if selected not in self._providers:
            raise ValueError(f"AI provider is not registered: {selected}")
        self.default_provider = selected

    def resolve(self, provider: str | None = None) -> AIGateway:
        selected = (provider or self.default_provider).strip().lower()
        gateway = self._providers.get(selected)
        if gateway is None:
            from ...core.errors import GatewayError

            raise GatewayError(
                stage="llm",
                provider=selected or "unknown",
                code="unsupported_provider",
                public_message="The selected brain provider is not available.",
                technical_message=f"AI provider is not registered: {selected!r}",
            )
        return gateway

    def catalog(self) -> list[dict[str, object]]:
        labels = {"xai": "Grok", "deepseek": "DeepSeek", "google": "Gemini"}
        return [
            {
                "provider": provider,
                "label": labels.get(provider, provider.title()),
                "model": gateway.model,
                "reasoning_setting": gateway.reasoning_setting,
                "configured": gateway.api_key_configured,
                "selected": provider == self.default_provider,
            }
            for provider, gateway in self._providers.items()
        ]


LLMGateway = AIGateway
