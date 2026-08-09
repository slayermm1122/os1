from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class STTWordTiming:
    text: str
    start_ms: float
    end_ms: float
    kind: str = "word"


@dataclass(frozen=True)
class STTResult:
    text: str
    raw: dict[str, Any]
    language_code: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class STTEvent:
    kind: Literal["session_started", "partial", "committed", "timing"]
    text: str = ""
    language_code: str | None = None
    request_id: str | None = None
    words: tuple[STTWordTiming, ...] = ()


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
        vad_threshold: float | None = None,
        vad_silence_threshold_secs: float | None = None,
        continuous: bool = False,
        detect_language: bool = False,
        filter_background_audio: bool = False,
        keyterms: tuple[str, ...] | None = None,
    ) -> AsyncIterator[STTEvent]: ...
