from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class TTSAlignment:
    chars: tuple[str, ...]
    char_start_times_ms: tuple[float, ...]
    char_durations_ms: tuple[float, ...]


@dataclass(frozen=True)
class TTSEvent:
    kind: Literal["audio", "complete"]
    audio: bytes = b""
    request_id: str | None = None
    trace_id: str | None = None
    character_cost: int | None = None
    alignment: TTSAlignment | None = None


class TTSGateway(Protocol):
    provider: str
    model: str
    http_output_format: str
    stream_output_format: str
    stream_sample_rate: int | None
    http_media_type: str
    stream_media_type: str
    api_key_configured: bool

    def resolve_voice_id(
        self,
        voice_id: str | None,
        voice_gender: str | None,
    ) -> str | None: ...

    def stream_http(
        self,
        text: str,
        *,
        api_key: str | None = None,
        voice_id: str | None = None,
    ) -> AsyncIterator[TTSEvent]: ...

    def stream_websocket(
        self,
        text_chunks: AsyncIterable[str],
        *,
        api_key: str | None = None,
        voice_id: str | None = None,
    ) -> AsyncIterator[TTSEvent]: ...


class TTSAdapter:
    """Request-scoped router for replaceable TTS provider gateways."""

    def __init__(
        self,
        providers: Iterable[TTSGateway],
        *,
        default_provider: str,
    ) -> None:
        self._providers: dict[str, TTSGateway] = {}
        for gateway in providers:
            provider = gateway.provider.strip().lower()
            if not provider:
                raise ValueError("TTS providers must have a non-empty provider name.")
            if provider in self._providers:
                raise ValueError(f"Duplicate TTS provider: {provider}")
            self._providers[provider] = gateway
        self.default_provider = default_provider.strip().lower()
        if self.default_provider not in self._providers:
            raise ValueError(f"Default TTS provider is not registered: {self.default_provider}")

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def resolve(self, provider: str | None = None) -> TTSGateway:
        selected = (provider or self.default_provider).strip().lower()
        gateway = self._providers.get(selected)
        if gateway is None:
            from ...core.errors import GatewayError

            raise GatewayError(
                stage="tts",
                provider=selected or "unknown",
                code="unsupported_provider",
                public_message="The selected voice provider is not available.",
                technical_message=f"TTS provider is not registered: {selected!r}",
            )
        return gateway

    def resolve_voice_id(
        self,
        provider: str | None,
        voice_id: str | None,
        voice_gender: str | None,
    ) -> str | None:
        return self.resolve(provider).resolve_voice_id(voice_id, voice_gender)

    def stream_http(
        self,
        text: str,
        *,
        provider: str | None = None,
        api_key: str | None = None,
        voice_id: str | None = None,
    ) -> AsyncIterator[TTSEvent]:
        return self.resolve(provider).stream_http(
            text,
            api_key=api_key,
            voice_id=voice_id,
        )

    def stream_websocket(
        self,
        text_chunks: AsyncIterable[str],
        *,
        provider: str | None = None,
        api_key: str | None = None,
        voice_id: str | None = None,
    ) -> AsyncIterator[TTSEvent]:
        return self.resolve(provider).stream_websocket(
            text_chunks,
            api_key=api_key,
            voice_id=voice_id,
        )
