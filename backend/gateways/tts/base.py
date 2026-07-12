from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
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
