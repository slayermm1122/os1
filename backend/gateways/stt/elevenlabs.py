from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any
from urllib.parse import urlencode

import httpx
import websockets

from ...config import Settings
from ...core.errors import GatewayError
from .base import STTEvent, STTResult


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
                send_finished = asyncio.Event()
                received_commit = False

                async def send_audio() -> None:
                    try:
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
                    finally:
                        send_finished.set()

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
                                raise GatewayError(
                                    stage="stt",
                                    provider=self.provider,
                                    code=message_type,
                                    public_message="Realtime transcription failed.",
                                    technical_message=str(payload.get("message") or message_type),
                                    retryable=message_type in {"rate_limited", "queue_overflow", "resource_exhausted"},
                                    request_id=request_id,
                                )
                            if message_type == "session_started":
                                yield STTEvent(kind="session_started", request_id=request_id)
                            elif message_type == "partial_transcript":
                                yield STTEvent(kind="partial", text=str(payload.get("text") or ""), request_id=request_id)
                            elif message_type in {"committed_transcript", "committed_transcript_with_timestamps"}:
                                received_commit = True
                                yield STTEvent(
                                    kind="committed",
                                    text=str(payload.get("text") or "").strip(),
                                    language_code=str(payload.get("language_code") or "") or None,
                                    request_id=request_id,
                                )
                                if send_finished.is_set():
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
        code = "authentication_failed" if status in {401, 403} else "rate_limited" if status == 429 else "upstream_rejected"
        return GatewayError(
            stage=stage,
            provider="elevenlabs",
            code=code,
            public_message="ElevenLabs rejected the transcription request.",
            technical_message=str(exc),
            retryable=status >= 500 or status == 429,
            upstream_status=status,
            request_id=exc.response.headers.get("request-id") or exc.response.headers.get("x-request-id"),
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
