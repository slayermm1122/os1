from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterable, AsyncIterator
from urllib.parse import urlencode

import httpx
import websockets

from ...config import Settings
from ...core.errors import GatewayError, parse_provider_error
from .base import TTSEvent


class ElevenLabsTTSGateway:
    provider = "elevenlabs"
    base_url = "https://api.elevenlabs.io/v1"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.elevenlabs_tts_model
        self.http_output_format = settings.elevenlabs_output_format
        self.stream_output_format = settings.elevenlabs_stream_output_format
        self.stream_sample_rate = settings.tts_stream_sample_rate

    def _require_api_key(self, api_key: str | None) -> str:
        resolved = (api_key or self.settings.elevenlabs_api_key).strip()
        if not resolved:
            raise GatewayError(
                stage="tts",
                provider=self.provider,
                code="api_key_missing",
                public_message="ElevenLabs API key is required.",
                technical_message="ELEVENLABS_API_KEY is not configured.",
            )
        return resolved

    def _resolve_voice_id(self, voice_id: str | None) -> str:
        resolved = (voice_id or self.settings.elevenlabs_voice_id).strip()
        if not resolved:
            raise GatewayError(
                stage="tts",
                provider=self.provider,
                code="voice_missing",
                public_message="ElevenLabs voice is required.",
                technical_message="ELEVENLABS_VOICE_ID is not configured.",
            )
        return resolved

    async def stream_http(
        self,
        text: str,
        *,
        api_key: str | None = None,
        voice_id: str | None = None,
    ) -> AsyncIterator[TTSEvent]:
        key = self._require_api_key(api_key)
        selected_voice = self._resolve_voice_id(voice_id)
        url = f"{self.base_url}/text-to-speech/{selected_voice}/stream"
        payload = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.75, "speed": 1.0},
        }
        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=self.settings.upstream_read_timeout_seconds,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    url,
                    params={
                        "output_format": self.http_output_format,
                        "enable_logging": str(self.settings.elevenlabs_enable_logging).lower(),
                    },
                    headers={"xi-api-key": key, "Content-Type": "application/json"},
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
                    trace_id = response.headers.get("x-trace-id")
                    character_cost = _integer(response.headers.get("character-cost"))
                    async with asyncio.timeout(self.settings.upstream_stream_timeout_seconds):
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                yield TTSEvent(kind="audio", audio=chunk)
                    yield TTSEvent(
                        kind="complete",
                        request_id=request_id,
                        trace_id=trace_id,
                        character_cost=character_cost,
                    )
        except GatewayError:
            raise
        except Exception as exc:
            raise _gateway_error(exc) from exc

    async def stream_websocket(
        self,
        text_chunks: AsyncIterable[str],
        *,
        api_key: str | None = None,
        voice_id: str | None = None,
    ) -> AsyncIterator[TTSEvent]:
        key = self._require_api_key(api_key)
        selected_voice = self._resolve_voice_id(voice_id)
        query = urlencode(
            {
                "model_id": self.model,
                "output_format": self.stream_output_format,
                "auto_mode": "true",
                "enable_logging": str(self.settings.elevenlabs_enable_logging).lower(),
            }
        )
        uri = (
            "wss://api.elevenlabs.io/v1/text-to-speech/"
            f"{selected_voice}/stream-input?{query}"
        )

        try:
            async with websockets.connect(
                uri,
                max_size=8 * 1024 * 1024,
                open_timeout=self.settings.upstream_connect_timeout_seconds,
                close_timeout=5,
            ) as websocket:
                response_headers = getattr(getattr(websocket, "response", None), "headers", {})
                request_id = response_headers.get("request-id") or response_headers.get("x-request-id")
                trace_id = response_headers.get("x-trace-id")
                await websocket.send(
                    json.dumps(
                        {
                            "text": " ",
                            "xi_api_key": key,
                            "voice_settings": {
                                "stability": 0.45,
                                "similarity_boost": 0.75,
                                "speed": 1.0,
                            },
                            "generation_config": {"chunk_length_schedule": [50, 90, 140, 200]},
                        }
                    )
                )

                async def send_text() -> None:
                    pending = ""
                    async for chunk in text_chunks:
                        chunk = chunk.strip()
                        if not chunk:
                            continue
                        if pending:
                            await websocket.send(json.dumps({"text": pending + " "}))
                        pending = chunk
                    if pending:
                        await websocket.send(json.dumps({"text": pending + " ", "flush": True}))
                    await websocket.send(json.dumps({"text": ""}))

                send_task = asyncio.create_task(send_text())
                send_results: list[object] = []
                received_final = False
                try:
                    async with asyncio.timeout(self.settings.upstream_stream_timeout_seconds):
                        async for message in websocket:
                            if isinstance(message, bytes):
                                yield TTSEvent(kind="audio", audio=message)
                                continue
                            payload = json.loads(message)
                            if payload.get("error"):
                                provider_message = str(payload.get("message") or payload.get("error"))
                                raise GatewayError(
                                    stage="tts",
                                    provider=self.provider,
                                    code=str(payload.get("error")),
                                    public_message="ElevenLabs rejected the selected voice.",
                                    technical_message=provider_message,
                                    retryable=False,
                                    request_id=request_id,
                                    provider_detail={
                                        "type": str(payload.get("error")),
                                        "code": str(payload.get("error")),
                                        "message": provider_message,
                                    },
                                )
                            audio = payload.get("audio")
                            if audio:
                                yield TTSEvent(kind="audio", audio=base64.b64decode(audio))
                            if payload.get("isFinal"):
                                received_final = True
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
                if not received_final:
                    raise GatewayError(
                        stage="tts",
                        provider=self.provider,
                        code="stream_closed",
                        public_message="ElevenLabs speech ended before completion.",
                        technical_message="Streaming TTS WebSocket closed without an isFinal event.",
                        retryable=True,
                        request_id=request_id,
                    )
                yield TTSEvent(kind="complete", request_id=request_id, trace_id=trace_id)
        except GatewayError:
            raise
        except Exception as exc:
            raise _gateway_error(exc) from exc


def _gateway_error(exc: Exception) -> GatewayError:
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
            stage="tts",
            provider="elevenlabs",
            code=code,
            public_message="ElevenLabs rejected the speech request.",
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
            stage="tts",
            provider="elevenlabs",
            code="timeout",
            public_message="ElevenLabs speech timed out.",
            technical_message=str(exc),
            retryable=True,
        )
    return GatewayError(
        stage="tts",
        provider="elevenlabs",
        code="transport_error",
        public_message="ElevenLabs speech is unavailable.",
        technical_message=str(exc),
        retryable=True,
    )


def _integer(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
