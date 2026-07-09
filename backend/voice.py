from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterable
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

import httpx
import websockets

from .config import Settings


class VoiceClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = "https://api.elevenlabs.io/v1"

    def _require_api_key(self, api_key: str | None = None) -> str:
        resolved_api_key = (api_key or self.settings.elevenlabs_api_key).strip()
        if not resolved_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not configured.")
        return resolved_api_key

    async def transcribe_upload(
        self,
        *,
        filename: str,
        content_type: str | None,
        data: bytes,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        resolved_api_key = self._require_api_key(api_key)
        form: dict[str, str] = {"model_id": self.settings.elevenlabs_stt_model}
        if self.settings.elevenlabs_stt_language_code:
            form["language_code"] = self.settings.elevenlabs_stt_language_code

        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=self.settings.upstream_read_timeout_seconds,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self.base_url}/speech-to-text",
                headers={"xi-api-key": resolved_api_key},
                data=form,
                files={
                    "file": (
                        filename or "speech.webm",
                        data,
                        content_type or "application/octet-stream",
                    )
                },
            )
        response.raise_for_status()
        payload = response.json()
        return {
            "text": payload.get("text", "").strip(),
            "raw": payload,
        }

    async def stream_tts(
        self,
        text: str,
        *,
        api_key: str | None = None,
    ) -> AsyncIterator[bytes]:
        resolved_api_key = self._require_api_key(api_key)
        if not self.settings.elevenlabs_voice_id:
            raise RuntimeError("ELEVENLABS_VOICE_ID is not configured.")

        url = (
            f"{self.base_url}/text-to-speech/"
            f"{self.settings.elevenlabs_voice_id}/stream"
        )
        params = {"output_format": self.settings.elevenlabs_output_format}
        payload = {
            "text": text,
            "model_id": self.settings.elevenlabs_tts_model,
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.75,
                "speed": 1.0,
            },
        }

        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=self.settings.upstream_read_timeout_seconds,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                url,
                params=params,
                headers={
                    "xi-api-key": resolved_api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                response.raise_for_status()
                async with asyncio.timeout(self.settings.upstream_stream_timeout_seconds):
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            yield chunk

    async def stream_tts_websocket(
        self,
        text_chunks: AsyncIterable[str],
        *,
        api_key: str | None = None,
    ) -> AsyncIterator[bytes]:
        resolved_api_key = self._require_api_key(api_key)
        if not self.settings.elevenlabs_voice_id:
            raise RuntimeError("ELEVENLABS_VOICE_ID is not configured.")

        query = urlencode(
            {
                "model_id": self.settings.elevenlabs_tts_model,
                "output_format": self.settings.elevenlabs_stream_output_format,
                "auto_mode": "true",
            }
        )
        uri = (
            "wss://api.elevenlabs.io/v1/text-to-speech/"
            f"{self.settings.elevenlabs_voice_id}/stream-input?{query}"
        )

        async with websockets.connect(
            uri,
            max_size=8 * 1024 * 1024,
            open_timeout=self.settings.upstream_connect_timeout_seconds,
            close_timeout=5,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "text": " ",
                        "xi_api_key": resolved_api_key,
                        "voice_settings": {
                            "stability": 0.45,
                            "similarity_boost": 0.75,
                            "speed": 1.0,
                        },
                        "generation_config": {
                            "chunk_length_schedule": [50, 90, 140, 200],
                        },
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
            try:
                async with asyncio.timeout(self.settings.upstream_stream_timeout_seconds):
                    async for message in websocket:
                        if isinstance(message, bytes):
                            yield message
                            continue

                        payload = json.loads(message)
                        audio = payload.get("audio")
                        if audio:
                            yield base64.b64decode(audio)
                        if payload.get("isFinal"):
                            break
            finally:
                if not send_task.done():
                    send_task.cancel()
                await asyncio.gather(send_task, return_exceptions=True)
