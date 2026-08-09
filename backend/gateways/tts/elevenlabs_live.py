from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterable, AsyncIterator
from urllib.parse import urlencode

import websockets

from ...config import Settings
from ...core.errors import GatewayError
from .base import TTSEvent


_TEXT_SENDER_DONE = object()


class ElevenLabsMultiContextSession:
    """One voice-specific ElevenLabs socket shared by all turns in a live tab."""

    KEEPALIVE_CONTEXT = "os1-session-keepalive"

    def __init__(self, settings: Settings, voice_id: str) -> None:
        self.settings = settings
        self.voice_id = voice_id.strip()
        self.websocket = None
        self.request_id: str | None = None
        self.trace_id: str | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._context_lock = asyncio.Lock()
        self._contexts: dict[str, asyncio.Queue[TTSEvent | BaseException | object | None]] = {}
        self._closed = False

    async def connect(self) -> None:
        if self.websocket is not None and not self._closed:
            return
        key = self.settings.elevenlabs_api_key.strip()
        if not key:
            raise GatewayError(
                stage="tts",
                provider="elevenlabs",
                code="api_key_missing",
                public_message="ElevenLabs API key is required.",
            )
        if not self.voice_id:
            raise GatewayError(
                stage="tts",
                provider="elevenlabs",
                code="voice_missing",
                public_message="Choose an ElevenLabs voice.",
            )
        query = urlencode(
            {
                "model_id": "eleven_flash_v2_5",
                "output_format": self.settings.elevenlabs_stream_output_format,
                "inactivity_timeout": "180",
                "sync_alignment": "true",
                "auto_mode": "true",
                "enable_logging": str(self.settings.elevenlabs_enable_logging).lower(),
            }
        )
        uri = (
            "wss://api.elevenlabs.io/v1/text-to-speech/"
            f"{self.voice_id}/multi-stream-input?{query}"
        )
        try:
            self.websocket = await websockets.connect(
                uri,
                additional_headers={"xi-api-key": key},
                max_size=8 * 1024 * 1024,
                open_timeout=self.settings.upstream_connect_timeout_seconds,
                close_timeout=5,
            )
        except Exception as exc:
            raise GatewayError(
                stage="tts",
                provider="elevenlabs",
                code="connect_timeout" if isinstance(exc, TimeoutError) else "transport_error",
                public_message="Could not connect to ElevenLabs live speech.",
                technical_message=str(exc),
                retryable=True,
            ) from exc
        response_headers = getattr(getattr(self.websocket, "response", None), "headers", {})
        self.request_id = response_headers.get("request-id") or response_headers.get("x-request-id")
        self.trace_id = response_headers.get("x-trace-id")
        self._closed = False
        self._receiver_task = asyncio.create_task(self._receive(), name="elevenlabs-multi-receive")
        await self._send(
            {
                "text": " ",
                "context_id": self.KEEPALIVE_CONTEXT,
                **self._initial_context_options(),
            }
        )
        self._keepalive_task = asyncio.create_task(
            self._keepalive(),
            name="elevenlabs-multi-keepalive",
        )

    async def stream_context(
        self,
        text_chunks: AsyncIterable[str],
        *,
        context_id: str,
    ) -> AsyncIterator[TTSEvent]:
        await self.connect()
        queue: asyncio.Queue[TTSEvent | BaseException | object | None] = asyncio.Queue()
        async with self._context_lock:
            if len(self._contexts) >= 4:
                raise GatewayError(
                    stage="tts",
                    provider="elevenlabs",
                    code="too_many_contexts",
                    public_message="Too many live speech contexts are active.",
                )
            self._contexts[context_id] = queue

        sent_chunks = 0
        sender_done = asyncio.Event()
        remote_close_sent = False
        final_event: TTSEvent | None = None

        async def send_text() -> None:
            nonlocal sent_chunks, remote_close_sent
            try:
                async for chunk in text_chunks:
                    text = chunk.strip()
                    if not text:
                        continue
                    payload: dict[str, object] = {
                        "context_id": context_id,
                        "text": f"{text} ",
                        "flush": True,
                    }
                    if sent_chunks == 0:
                        payload.update(self._initial_context_options())
                    sent_chunks += 1
                    await self._send(payload)
                if sent_chunks:
                    await self._send({"context_id": context_id, "close_context": True})
                    remote_close_sent = True
            except BaseException as exc:
                queue.put_nowait(exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
            finally:
                sender_done.set()
                queue.put_nowait(_TEXT_SENDER_DONE)

        sender_task = asyncio.create_task(
            send_text(),
            name=f"elevenlabs-context-send-{context_id}",
        )
        try:
            while True:
                item = await queue.get()
                if item is _TEXT_SENDER_DONE:
                    if sent_chunks == 0:
                        return
                    if final_event is not None:
                        yield final_event
                        break
                    continue
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                if item.kind == "complete":
                    final_event = item
                    if sender_done.is_set():
                        yield item
                        break
                    continue
                yield item
        finally:
            if not sender_task.done():
                sender_task.cancel()
            await asyncio.gather(sender_task, return_exceptions=True)
            if remote_close_sent or sent_chunks == 0:
                await self._release_context(context_id)
            else:
                await self.close_context(context_id)

    async def _release_context(self, context_id: str) -> bool:
        async with self._context_lock:
            queue = self._contexts.pop(context_id, None)
        if queue is not None:
            queue.put_nowait(None)
        return queue is not None

    async def close_context(self, context_id: str) -> None:
        released = await self._release_context(context_id)
        if released and self.websocket is not None and not self._closed:
            try:
                await self._send({"context_id": context_id, "close_context": True})
            except Exception:
                pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        websocket = self.websocket
        self.websocket = None
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        if websocket is not None:
            try:
                async with self._send_lock:
                    await websocket.send(json.dumps({"close_socket": True}))
            except Exception:
                pass
            try:
                await websocket.close()
            except Exception:
                pass
        if self._receiver_task and not self._receiver_task.done():
            self._receiver_task.cancel()
        await asyncio.gather(
            *(task for task in (self._keepalive_task, self._receiver_task) if task is not None),
            return_exceptions=True,
        )
        async with self._context_lock:
            queues = list(self._contexts.values())
            self._contexts.clear()
        for queue in queues:
            queue.put_nowait(None)

    async def _send(self, payload: dict[str, object]) -> None:
        if self.websocket is None or self._closed:
            raise GatewayError(
                stage="tts",
                provider="elevenlabs",
                code="stream_closed",
                public_message="ElevenLabs live speech disconnected.",
                retryable=True,
            )
        async with self._send_lock:
            await self.websocket.send(json.dumps(payload))

    async def _receive(self) -> None:
        try:
            async for message in self.websocket:
                if isinstance(message, bytes):
                    continue
                payload = json.loads(message)
                if payload.get("error"):
                    message_text = str(payload.get("message") or payload.get("error"))
                    raise GatewayError(
                        stage="tts",
                        provider="elevenlabs",
                        code=str(payload.get("error")),
                        public_message="ElevenLabs rejected live speech.",
                        technical_message=message_text,
                    )
                context_id = str(payload.get("contextId") or payload.get("context_id") or "")
                if not context_id or context_id == self.KEEPALIVE_CONTEXT:
                    continue
                async with self._context_lock:
                    queue = self._contexts.get(context_id)
                if queue is None:
                    continue
                audio = payload.get("audio")
                if audio:
                    from .elevenlabs import _parse_alignment

                    alignment = _parse_alignment(payload.get("alignment"))
                    if alignment is None:
                        alignment = _parse_alignment(payload.get("normalizedAlignment"))
                    queue.put_nowait(
                        TTSEvent(
                            kind="audio",
                            audio=base64.b64decode(audio),
                            alignment=alignment,
                            request_id=self.request_id,
                            trace_id=self.trace_id,
                        )
                    )
                if payload.get("isFinal") or payload.get("is_final"):
                    queue.put_nowait(
                        TTSEvent(
                            kind="complete",
                            request_id=self.request_id,
                            trace_id=self.trace_id,
                        )
                    )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure: BaseException = exc
        else:
            failure = GatewayError(
                stage="tts",
                provider="elevenlabs",
                code="stream_closed",
                public_message="ElevenLabs live speech disconnected.",
                retryable=True,
            )
        async with self._context_lock:
            queues = list(self._contexts.values())
        for queue in queues:
            queue.put_nowait(failure)

    async def _keepalive(self) -> None:
        try:
            while True:
                await asyncio.sleep(15)
                await self._send({"context_id": self.KEEPALIVE_CONTEXT, "text": ""})
        except asyncio.CancelledError:
            raise

    def _initial_context_options(self) -> dict[str, object]:
        options: dict[str, object] = {
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.75,
                "speed": 1.0,
            },
            "generation_config": {"chunk_length_schedule": [50, 90, 140, 200]},
        }
        dictionary_id = self.settings.elevenlabs_pronunciation_dictionary_id.strip()
        version_id = self.settings.elevenlabs_pronunciation_dictionary_version_id.strip()
        if dictionary_id and version_id:
            options["pronunciation_dictionary_locators"] = [
                {
                    "pronunciation_dictionary_id": dictionary_id,
                    "version_id": version_id,
                }
            ]
        return options
