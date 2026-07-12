from __future__ import annotations

import asyncio
import base64
import json
import math
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any
from urllib.parse import urlencode

import httpx
import websockets

from ...config import Settings
from ...core.errors import GatewayError, parse_provider_error
from .base import STTEvent, STTResult, STTWordTiming


_ERROR_TYPES = {
    "auth_error",
    "quota_exceeded",
    "transcriber_error",
    "input_error",
    "error",
    "unaccepted_terms",
    "rate_limited",
    "queue_overflow",
    "resource_exhausted",
    "session_time_limit_exceeded",
    "chunk_size_exceeded",
    "insufficient_audio_activity",
}


class ElevenLabsSTTGateway:
    provider = "elevenlabs"
    base_url = "https://api.elevenlabs.io/v1"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.upload_model = settings.elevenlabs_stt_model
        self.realtime_model = settings.elevenlabs_realtime_stt_model

    def _require_api_key(self, api_key: str | None) -> str:
        resolved = (api_key or self.settings.elevenlabs_api_key).strip()
        if not resolved:
            raise GatewayError(
                stage="stt",
                provider=self.provider,
                code="api_key_missing",
                public_message="ElevenLabs API key is required.",
                technical_message="ELEVENLABS_API_KEY is not configured.",
            )
        return resolved

    async def transcribe_upload(
        self,
        *,
        filename: str,
        content_type: str | None,
        data: bytes,
        api_key: str | None = None,
    ) -> STTResult:
        resolved_key = self._require_api_key(api_key)
        form: dict[str, str] = {"model_id": self.upload_model}
        if self.settings.elevenlabs_stt_language_code:
            form["language_code"] = self.settings.elevenlabs_stt_language_code
        timeout = _timeout(self.settings)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self.base_url}/speech-to-text",
                    params={"enable_logging": str(self.settings.elevenlabs_enable_logging).lower()},
                    headers={"xi-api-key": resolved_key},
                    data=form,
                    files={"file": (filename or "speech.webm", data, content_type or "application/octet-stream")},
                )
            response.raise_for_status()
        except Exception as exc:
            raise _gateway_error(exc, stage="stt") from exc
        payload: dict[str, Any] = response.json()
        return STTResult(
            text=str(payload.get("text") or "").strip(),
            raw=payload,
            language_code=str(payload.get("language_code") or "") or None,
            request_id=response.headers.get("request-id") or response.headers.get("x-request-id"),
        )

    async def stream_realtime(
        self,
        audio_chunks: AsyncIterable[bytes],
        *,
        sample_rate: int,
        api_key: str | None = None,
    ) -> AsyncIterator[STTEvent]:
        resolved_key = self._require_api_key(api_key)
        audio_format = self.settings.elevenlabs_realtime_stt_audio_format
        if not audio_format.startswith("pcm_"):
            audio_format = f"pcm_{sample_rate}"
        query: dict[str, str] = {
            "model_id": self.realtime_model,
            "audio_format": audio_format,
            "commit_strategy": "manual",
            "include_timestamps": "true",
            "timestamps_granularity": "word",
            "enable_logging": str(self.settings.elevenlabs_enable_logging).lower(),
        }
        if self.settings.elevenlabs_stt_language_code:
            query["language_code"] = self.settings.elevenlabs_stt_language_code
        uri = f"wss://api.elevenlabs.io/v1/speech-to-text/realtime?{urlencode(query)}"

        connected = False
        try:
            async with websockets.connect(
                uri,
                additional_headers={"xi-api-key": resolved_key},
                max_size=8 * 1024 * 1024,
                open_timeout=self.settings.upstream_connect_timeout_seconds,
                close_timeout=5,
            ) as websocket:
                connected = True
                received_commit = False
                last_committed_text = ""

                async def send_audio() -> None:
                    async for chunk in audio_chunks:
                        if not chunk:
                            continue
                        await websocket.send(
                            json.dumps(
                                {
                                    "message_type": "input_audio_chunk",
                                    "audio_base_64": base64.b64encode(chunk).decode("ascii"),
                                    "sample_rate": sample_rate,
                                }
                            )
                        )
                    await websocket.send(
                        json.dumps(
                            {
                                "message_type": "input_audio_chunk",
                                "audio_base_64": "",
                                "sample_rate": sample_rate,
                                "commit": True,
                            }
                        )
                    )

                send_task = asyncio.create_task(send_audio())
                send_results: list[object] = []
                try:
                    async with asyncio.timeout(self.settings.upstream_stream_timeout_seconds):
                        async for message in websocket:
                            if isinstance(message, bytes):
                                continue
                            payload = json.loads(message)
                            message_type = str(payload.get("message_type") or payload.get("type") or "")
                            request_id = str(payload.get("session_id") or payload.get("request_id") or "") or None
                            if message_type in _ERROR_TYPES:
                                provider_message = str(payload.get("message") or message_type)
                                raise GatewayError(
                                    stage="stt",
                                    provider=self.provider,
                                    code=message_type,
                                    public_message="Realtime transcription failed.",
                                    technical_message=provider_message,
                                    retryable=message_type in {"rate_limited", "queue_overflow", "resource_exhausted"},
                                    request_id=request_id,
                                    provider_detail={
                                        "type": message_type,
                                        "code": message_type,
                                        "message": provider_message,
                                    },
                                )
                            if message_type == "session_started":
                                yield STTEvent(kind="session_started", request_id=request_id)
                            elif message_type == "partial_transcript":
                                yield STTEvent(kind="partial", text=str(payload.get("text") or ""), request_id=request_id)
                            elif message_type == "committed_transcript":
                                received_commit = True
                                last_committed_text = str(payload.get("text") or "").strip()
                                yield STTEvent(
                                    kind="committed",
                                    text=last_committed_text,
                                    language_code=str(payload.get("language_code") or "") or None,
                                    request_id=request_id,
                                )
                            elif message_type == "committed_transcript_with_timestamps":
                                received_commit = True
                                committed_text = str(payload.get("text") or "").strip()
                                if committed_text and committed_text != last_committed_text:
                                    last_committed_text = committed_text
                                    yield STTEvent(
                                        kind="committed",
                                        text=committed_text,
                                        language_code=str(payload.get("language_code") or "") or None,
                                        request_id=request_id,
                                    )
                                words = _parse_word_timings(payload.get("words"))
                                if words:
                                    yield STTEvent(
                                        kind="timing",
                                        text=committed_text or last_committed_text,
                                        language_code=str(payload.get("language_code") or "") or None,
                                        request_id=request_id,
                                        words=words,
                                    )
                                break
                finally:
                    if not send_task.done():
                        send_task.cancel()
                    send_results = await asyncio.gather(send_task, return_exceptions=True)
                send_error = send_results[0] if send_results else None
                if isinstance(send_error, BaseException) and not isinstance(
                    send_error,
                    asyncio.CancelledError,
                ):
                    raise send_error
                if not received_commit:
                    raise GatewayError(
                        stage="stt",
                        provider=self.provider,
                        code="stream_closed",
                        public_message="ElevenLabs transcription ended before committing a transcript.",
                        technical_message="Realtime STT WebSocket closed without a committed transcript event.",
                        retryable=True,
                    )
        except GatewayError:
            raise
        except Exception as exc:
            raise _gateway_error(exc, stage="stt", connecting=not connected) from exc


def _timeout(settings: Settings) -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.upstream_connect_timeout_seconds,
        read=settings.upstream_read_timeout_seconds,
        write=settings.upstream_write_timeout_seconds,
        pool=settings.upstream_pool_timeout_seconds,
    )


def _gateway_error(exc: Exception, *, stage: str, connecting: bool = False) -> GatewayError:
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        provider_detail = parse_provider_error(exc.response)
        provider_code = (provider_detail.get("status") or provider_detail.get("code") or "").lower()
        if provider_code == "quota_exceeded" or status == 402:
            code = "payment_required"
        elif status == 401:
            code = "authentication_failed"
        elif status == 403:
            code = "authorization_failed"
        elif status == 429:
            code = "rate_limited"
        else:
            code = "upstream_rejected"
        return GatewayError(
            stage=stage,
            provider="elevenlabs",
            code=code,
            public_message="ElevenLabs rejected the transcription request.",
            technical_message=(
                f"{exc}; provider: {provider_detail.get('message')}"
                if provider_detail.get("message")
                else str(exc)
            ),
            retryable=status >= 500 or status == 429,
            upstream_status=status,
            request_id=exc.response.headers.get("request-id") or exc.response.headers.get("x-request-id"),
            provider_detail=provider_detail,
        )
    if isinstance(exc, (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError)):
        return GatewayError(
            stage=stage,
            provider="elevenlabs",
            code="connect_timeout" if connecting else "timeout",
            public_message=(
                "Could not connect to ElevenLabs realtime transcription. Please try again."
                if connecting
                else "ElevenLabs transcription timed out."
            ),
            technical_message=str(exc),
            retryable=True,
        )
    return GatewayError(
        stage=stage,
        provider="elevenlabs",
        code="transport_error",
        public_message="ElevenLabs transcription is unavailable.",
        technical_message=str(exc),
        retryable=True,
    )


def _parse_word_timings(value: object) -> tuple[STTWordTiming, ...]:
    if not isinstance(value, list):
        return ()
    words: list[STTWordTiming] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        start = _finite_number(item.get("start"))
        end = _finite_number(item.get("end"))
        text = str(item.get("text") or "")
        if start is None or end is None or end < start or not text:
            continue
        words.append(
            STTWordTiming(
                text=text,
                start_ms=start * 1000,
                end_ms=end * 1000,
                kind=str(item.get("type") or "word"),
            )
        )
    return tuple(words)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None
