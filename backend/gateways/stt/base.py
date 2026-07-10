from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class STTResult:
    text: str
    raw: dict[str, Any]
    language_code: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class STTEvent:
    kind: Literal["session_started", "partial", "committed"]
    text: str = ""
    language_code: str | None = None
    request_id: str | None = None


class STTGateway(Protocol):
    provider: str
    upload_model: str
    realtime_model: str

    async def transcribe_upload(
        self,
        *,
        filename: str,
        content_type: str | None,
        data: bytes,
        api_key: str | None = None,
    ) -> STTResult: ...

    def stream_realtime(
        self,
        audio_chunks: AsyncIterable[bytes],
        *,
        sample_rate: int,
        api_key: str | None = None,
    ) -> AsyncIterator[STTEvent]: ...
