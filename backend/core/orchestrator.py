from __future__ import annotations

import asyncio
import base64
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass

from ..config import Settings
from ..gateways.ai import AIGateway, AIRequest, AIStreamEvent
from ..gateways.stt import STTEvent, STTGateway, STTResult
from ..gateways.tts import TTSAdapter, TTSEvent, TTSGateway
from ..telemetry.sqlite_store import SQLiteTelemetryRecorder, TurnTrace, utc_now
from .captions import caption_payload
from .chunking import pop_ready_speech_chunks
from .errors import ErrorInfo, GatewayError, error_info
from .messages import build_messages
from .sessions import KVConversationStore


@dataclass(frozen=True)
class PipelineEvent:
    event: str
    data: dict[str, object]


@dataclass
class LiveTurnState:
    user_text: str
    model_user_text: str = ""
    assistant_text: str = ""
    context_id: str = ""
    response_language: str = "en"
    completed: bool = False
    history_committed: bool = False
    trace: TurnTrace | None = None


class ObservedError(Exception):
    def __init__(self, info: ErrorInfo) -> None:
        self.info = info
        super().__init__(info.public_message)


class TurnOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        llm: AIGateway,
        stt: STTGateway,
        tts: TTSAdapter,
        sessions: KVConversationStore,
        telemetry: SQLiteTelemetryRecorder,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.stt = stt
        self.tts = tts
        self.sessions = sessions
        self.telemetry = telemetry

    def new_trace(self, *, session_id: str, kind: str) -> TurnTrace:
        return self.telemetry.new_turn(session_id=session_id, kind=kind)

    def event(self, trace: TurnTrace, event: str, data: dict[str, object] | None = None) -> PipelineEvent:
        payload = {"turn_id": trace.turn_id, "session_id": trace.session_id}
        if data:
            payload.update(data)
        return PipelineEvent(event=event, data=payload)

    async def transcribe_upload_only(
        self,
        trace: TurnTrace,
        *,
        filename: str,
        content_type: str | None,
        data: bytes,
        api_key: str | None,
    ) -> STTResult:
        try:
            result = await self._transcribe_upload(
                trace,
                filename=filename,
                content_type=content_type,
                data=data,
                api_key=api_key,
            )
        except asyncio.CancelledError:
            trace.finish("cancelled")
            raise
        except Exception:
            trace.finish("failed")
            raise
        trace.update_text(user_text=result.text)
        trace.finish("success", response_complete=True)
        return result

    async def stream_upload_turn(
        self,
        trace: TurnTrace,
        *,
        filename: str,
        content_type: str | None,
        data: bytes,
        brain_api_key: str | None,
        voice_api_key: str | None,
        voice_id: str | None,
        tts_provider: str | None = None,
        stt_api_key: str | None = None,
        tts_api_key: str | None = None,
    ) -> AsyncIterator[PipelineEvent]:
        selected_stt_key = stt_api_key if stt_api_key is not None else voice_api_key
        selected_tts_key = tts_api_key if tts_api_key is not None else voice_api_key
        yield self.event(trace, "status", {"state": "transcribing"})
        try:
            result = await self._transcribe_upload(
                trace,
                filename=filename,
                content_type=content_type,
                data=data,
                api_key=selected_stt_key,
            )
            user_text = result.text
            self._validate_user_text(user_text)
        except asyncio.CancelledError:
            trace.finish("cancelled")
            raise
        except Exception as exc:
            info = _observed_info(exc, default_stage="stt")
            if not isinstance(exc, ObservedError):
                self.telemetry.record_error(trace, info)
            trace.finish("failed")
            yield self.event(trace, "error", info.payload(trace.turn_id))
            return

        trace.update_text(user_text=user_text)
        yield self.event(trace, "transcript", {"text": user_text})
        async for event in self.stream_chat(
            trace,
            user_text=user_text,
            with_audio=True,
            brain_api_key=brain_api_key,
            voice_api_key=selected_tts_key,
            voice_id=voice_id,
            tts_provider=tts_provider,
            assistant_name=self.settings.assistant_name,
            user_name=self.settings.user_name,
            assistant_persona=self.settings.assistant_persona,
            response_language=resolve_response_language(
                self.settings.assistant_response_language,
                result.language_code,
            ),
        ):
            yield event

    async def stream_realtime_turn(
        self,
        trace: TurnTrace,
        audio_chunks: AsyncIterable[bytes],
        *,
        sample_rate: int,
        brain_api_key: str | None,
        voice_api_key: str | None,
        voice_id: str | None,
        tts_provider: str | None = None,
        stt_api_key: str | None = None,
        tts_api_key: str | None = None,
        vad_threshold: float | None = None,
        vad_silence_threshold_secs: float | None = None,
        assistant_name: str | None = None,
        user_name: str | None = None,
        assistant_persona: str | None = None,
        response_language: str | None = None,
    ) -> AsyncIterator[PipelineEvent]:
        selected_stt_key = stt_api_key if stt_api_key is not None else voice_api_key
        selected_tts_key = tts_api_key if tts_api_key is not None else voice_api_key
        yield self.event(trace, "status", {"state": "connecting_stt"})
        try:
            user_text = ""
            partial_text = ""
            detected_language: str | None = None
            stt_ready = False
            async for event in self._transcribe_realtime(
                trace,
                audio_chunks,
                sample_rate=sample_rate,
                api_key=selected_stt_key,
                vad_threshold=vad_threshold,
                vad_silence_threshold_secs=vad_silence_threshold_secs,
            ):
                if event.kind == "session_started":
                    stt_ready = True
                    yield self.event(trace, "stt_ready", {"state": "listening"})
                elif event.kind == "partial":
                    if not stt_ready:
                        stt_ready = True
                        yield self.event(trace, "stt_ready", {"state": "listening"})
                    partial_text = event.text.strip()
                    yield self.event(trace, "transcript_partial", {"text": partial_text})
                elif event.kind == "committed" and event.text.strip():
                    if not stt_ready:
                        stt_ready = True
                        yield self.event(trace, "stt_ready", {"state": "listening"})
                    user_text = f"{user_text} {event.text.strip()}".strip()
                    detected_language = event.language_code or detected_language
                    yield self.event(trace, "transcript", {"text": user_text})
                elif event.kind == "timing" and event.words:
                    yield self.event(
                        trace,
                        "transcript_timing",
                        {
                            "text": event.text,
                            "words": [
                                {
                                    "text": word.text,
                                    "start_ms": word.start_ms,
                                    "end_ms": word.end_ms,
                                    "kind": word.kind,
                                }
                                for word in event.words
                            ],
                        },
                    )
            user_text = user_text or partial_text
            self._validate_user_text(user_text)
        except asyncio.CancelledError:
            trace.finish("cancelled")
            raise
        except Exception as exc:
            info = _observed_info(exc, default_stage="stt")
            if not isinstance(exc, ObservedError):
                self.telemetry.record_error(trace, info)
            trace.finish("failed")
            yield self.event(trace, "error", info.payload(trace.turn_id))
            return

        # Tell the client to stop the mic after VAD (or manual) commit completes.
        yield self.event(
            trace,
            "recording_stopped",
            {"reason": "stt_complete"},
        )
        yield self.event(trace, "status", {"state": "transcribing"})
        trace.update_text(user_text=user_text)
        yield self.event(trace, "transcript", {"text": user_text})
        async for event in self.stream_chat(
            trace,
            user_text=user_text,
            with_audio=True,
            brain_api_key=brain_api_key,
            voice_api_key=selected_tts_key,
            voice_id=voice_id,
            tts_provider=tts_provider,
            assistant_name=assistant_name,
            user_name=user_name,
            assistant_persona=assistant_persona,
            response_language=response_language or resolve_response_language(
                self.settings.assistant_response_language,
                detected_language,
            ),
        ):
            yield event

    async def stream_chat(
        self,
        trace: TurnTrace,
        *,
        user_text: str,
        with_audio: bool,
        brain_api_key: str | None,
        voice_api_key: str | None,
        voice_id: str | None,
        tts_provider: str | None = None,
        assistant_name: str | None = None,
        user_name: str | None = None,
        assistant_persona: str | None = None,
        response_language: str | None = None,
    ) -> AsyncIterator[PipelineEvent]:
        trace.update_text(user_text=user_text)
        try:
            history = self.sessions.get_history(trace.session_id)
            messages = build_messages(
                system_prompt=self.settings.system_prompt,
                user_text=user_text,
                history=history,
                assistant_name=assistant_name or "",
                user_name=user_name or "",
                persona=assistant_persona or "default",
                response_language=response_language or resolve_response_language(
                    self.settings.assistant_response_language,
                    None,
                ),
            )
        except asyncio.CancelledError:
            trace.finish("cancelled")
            raise
        except Exception as exc:
            info = _observed_info(exc, default_stage="llm")
            if not isinstance(exc, ObservedError):
                self.telemetry.record_error(trace, info)
            trace.finish("failed")
            yield self.event(trace, "error", info.payload(trace.turn_id))
            return

        model_user_text = messages[-1]["content"]
        yield self.event(trace, "status", {"state": "thinking"})

        if with_audio:
            async for event in self._stream_chat_with_audio(
                trace,
                user_text=user_text,
                model_user_text=model_user_text,
                messages=messages,
                brain_api_key=brain_api_key,
                voice_api_key=voice_api_key,
                voice_id=voice_id,
                tts_provider=tts_provider,
            ):
                yield event
            return

        assistant_text = ""
        try:
            async for llm_event in self._stream_llm(trace, messages, api_key=brain_api_key):
                if llm_event.kind == "delta":
                    assistant_text += llm_event.text
                    trace.update_text(assistant_text=assistant_text)
                    yield self.event(trace, "delta", {"text": llm_event.text})
        except asyncio.CancelledError:
            trace.finish("cancelled", assistant_text=assistant_text)
            raise
        except Exception as exc:
            info = _observed_info(exc, default_stage="llm")
            trace.finish("failed", assistant_text=assistant_text)
            yield self.event(trace, "error", info.payload(trace.turn_id))
            return

        self.sessions.append_turn(trace.session_id, model_user_text, assistant_text)
        trace.finish("success", assistant_text=assistant_text, response_complete=True)
        yield self.event(trace, "done", {"text": assistant_text})

    async def stream_tts_http(
        self,
        trace: TurnTrace,
        *,
        text: str,
        api_key: str | None,
        voice_id: str | None,
        tts_provider: str | None = None,
    ) -> AsyncIterator[bytes]:
        tts = self.tts.resolve(tts_provider)
        call_id = uuid.uuid4().hex
        call_start = trace.offset_ms()
        audio_bytes = 0
        first_audio_ms: float | None = None
        metadata = TTSEvent(kind="complete")
        self.telemetry.start_tts_call(
            trace,
            call_id=call_id,
            provider=tts.provider,
            model=tts.model,
            voice_id=voice_id,
            output_format=tts.http_output_format,
            sample_rate=_sample_rate(tts.http_output_format),
        )
        trace.event("tts.first_text", stage="tts", metadata={"call_id": call_id})
        try:
            async for event in self.tts.stream_http(
                text,
                provider=tts.provider,
                api_key=api_key,
                voice_id=voice_id,
            ):
                if event.kind == "audio":
                    if first_audio_ms is None:
                        first_audio_ms = trace.offset_ms() - call_start
                        trace.event("tts.first_audio", stage="tts", metadata={"call_id": call_id})
                    audio_bytes += len(event.audio)
                    yield event.audio
                else:
                    metadata = event
        except asyncio.CancelledError:
            self.telemetry.finish_tts_call(
                call_id,
                status="cancelled",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                input_text=text,
                input_chars=len(text),
                input_chunks=1,
                first_text_ms=0.0,
                first_audio_ms=first_audio_ms,
                audio_bytes=audio_bytes,
            )
            trace.finish("cancelled")
            raise
        except Exception as exc:
            info = error_info(exc, default_stage="tts")
            self.telemetry.record_error(trace, info, call_id=call_id)
            self.telemetry.finish_tts_call(
                call_id,
                status="failed",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                input_text=text,
                input_chars=len(text),
                input_chunks=1,
                first_text_ms=0.0,
                first_audio_ms=first_audio_ms,
                audio_bytes=audio_bytes,
                error_id=info.error_id,
            )
            trace.finish("failed")
            raise ObservedError(info) from exc

        sample_rate = _sample_rate(tts.http_output_format)
        if audio_bytes == 0:
            info = error_info(
                GatewayError(
                    stage="tts",
                    provider=tts.provider,
                    code="empty_audio",
                    public_message="Voice service returned no audio.",
                    technical_message="HTTP TTS stream completed without audio bytes.",
                    retryable=True,
                    request_id=metadata.request_id,
                )
            )
            self.telemetry.record_error(trace, info, call_id=call_id)
            self.telemetry.finish_tts_call(
                call_id,
                status="failed",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                input_text=text,
                input_chars=len(text),
                input_chunks=1,
                first_text_ms=0.0,
                first_audio_ms=None,
                audio_bytes=0,
                character_cost=metadata.character_cost,
                provider_request_id=metadata.request_id,
                trace_id=metadata.trace_id,
                error_id=info.error_id,
            )
            trace.finish("failed")
            raise ObservedError(info)
        self.telemetry.finish_tts_call(
            call_id,
            status="success",
            completed_at=utc_now(),
            duration_ms=trace.offset_ms() - call_start,
            input_text=text,
            input_chars=len(text),
            input_chunks=1,
            first_text_ms=0.0,
            first_audio_ms=first_audio_ms,
            audio_bytes=audio_bytes,
            audio_duration_ms=_pcm_duration_ms(audio_bytes, sample_rate),
            character_cost=metadata.character_cost,
            provider_request_id=metadata.request_id,
            trace_id=metadata.trace_id,
        )
        trace.finish("success", response_complete=True)

    async def stream_live_chat(
        self,
        trace: TurnTrace,
        *,
        state: LiveTurnState,
        live_tts,
        response_language: str,
    ) -> AsyncIterator[PipelineEvent]:
        """Stream one answer through a context on a session-owned TTS connection.

        History is intentionally committed by the realtime session after browser
        playback completes, so interrupted turns can retain only audible text.
        """
        trace.update_text(user_text=state.user_text)
        history = self.sessions.get_history(trace.session_id)
        messages = build_messages(
            system_prompt=self.settings.system_prompt,
            user_text=state.user_text,
            history=history,
            assistant_name=self.settings.assistant_name,
            user_name=self.settings.user_name,
            persona=self.settings.assistant_persona,
            response_language=response_language,
        )
        state.model_user_text = messages[-1]["content"]
        state.response_language = response_language
        event_queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        speech_buffer = ""
        llm_failed = False
        tts_failed = False

        async def text_chunks() -> AsyncIterator[str]:
            while True:
                chunk = await text_queue.get()
                if chunk is None:
                    break
                yield chunk

        async def produce_llm() -> None:
            nonlocal speech_buffer, llm_failed
            try:
                async for llm_event in self._stream_llm(trace, messages, api_key=None):
                    if llm_event.kind != "delta":
                        continue
                    state.assistant_text += llm_event.text
                    trace.update_text(assistant_text=state.assistant_text)
                    await event_queue.put(
                        ("event", self.event(trace, "delta", {"text": llm_event.text}))
                    )
                    speech_buffer += llm_event.text
                    ready, speech_buffer = pop_ready_speech_chunks(speech_buffer)
                    for chunk in ready:
                        await text_queue.put(chunk)
                        await event_queue.put(
                            ("event", self.event(trace, "display", {"text": chunk}))
                        )
                final_chunk = speech_buffer.strip()
                if final_chunk:
                    await text_queue.put(final_chunk)
                    await event_queue.put(
                        ("event", self.event(trace, "display", {"text": final_chunk}))
                    )
                await event_queue.put(
                    ("event", self.event(trace, "done", {"text": state.assistant_text}))
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                llm_failed = True
                info = _observed_info(exc, default_stage="llm")
                await event_queue.put(
                    ("event", self.event(trace, "error", info.payload(trace.turn_id)))
                )
            finally:
                await text_queue.put(None)
                await event_queue.put(("complete", "llm"))

        async def produce_tts() -> None:
            nonlocal tts_failed
            call_id = uuid.uuid4().hex
            call_start = trace.offset_ms()
            input_chunks: list[str] = []
            audio_bytes = 0
            first_text_ms: float | None = None
            first_audio_ms: float | None = None
            self.telemetry.start_tts_call(
                trace,
                call_id=call_id,
                provider="elevenlabs",
                model="eleven_flash_v2_5",
                voice_id=self.settings.elevenlabs_voice_id,
                output_format=self.settings.elevenlabs_stream_output_format,
                sample_rate=_sample_rate(self.settings.elevenlabs_stream_output_format),
            )

            async def observed_chunks() -> AsyncIterator[str]:
                nonlocal first_text_ms
                async for chunk in text_chunks():
                    if first_text_ms is None:
                        first_text_ms = trace.offset_ms() - call_start
                        trace.event("tts.first_text", stage="tts", metadata={"call_id": call_id})
                    input_chunks.append(chunk)
                    yield chunk

            trace.event("tts.context_created", stage="tts", metadata={"context_id": state.context_id})
            try:
                async for output in live_tts.stream_context(
                    observed_chunks(),
                    context_id=state.context_id,
                ):
                    if output.kind != "audio":
                        continue
                    if first_audio_ms is None:
                        first_audio_ms = trace.offset_ms() - call_start
                        trace.event("tts.first_audio", stage="tts", metadata={"call_id": call_id})
                    audio_bytes += len(output.audio)
                    data: dict[str, object] = {
                        "audio": base64.b64encode(output.audio).decode("ascii"),
                        "mime_type": "audio/L16",
                        "format": self.settings.elevenlabs_stream_output_format,
                        "sample_rate": _sample_rate(
                            self.settings.elevenlabs_stream_output_format
                        ),
                        "context_id": state.context_id,
                    }
                    if output.alignment is not None:
                        data["alignment"] = caption_payload(output.alignment)
                    await event_queue.put(("event", self.event(trace, "audio", data)))
                await event_queue.put(("event", self.event(trace, "audio_done")))
            except asyncio.CancelledError:
                input_text, input_chars = _tts_input_metrics(input_chunks)
                self.telemetry.finish_tts_call(
                    call_id,
                    status="cancelled",
                    completed_at=utc_now(),
                    duration_ms=trace.offset_ms() - call_start,
                    input_text=input_text,
                    input_chars=input_chars,
                    input_chunks=len(input_chunks),
                    first_text_ms=first_text_ms,
                    first_audio_ms=first_audio_ms,
                    audio_bytes=audio_bytes,
                )
                raise
            except Exception as exc:
                tts_failed = True
                info = _observed_info(exc, default_stage="tts")
                self.telemetry.record_error(trace, info, call_id=call_id)
                input_text, input_chars = _tts_input_metrics(input_chunks)
                self.telemetry.finish_tts_call(
                    call_id,
                    status="failed",
                    completed_at=utc_now(),
                    duration_ms=trace.offset_ms() - call_start,
                    input_text=input_text,
                    input_chars=input_chars,
                    input_chunks=len(input_chunks),
                    first_text_ms=first_text_ms,
                    first_audio_ms=first_audio_ms,
                    audio_bytes=audio_bytes,
                )
                await event_queue.put(
                    ("event", self.event(trace, "tts_error", info.payload(trace.turn_id)))
                )
            else:
                input_text, input_chars = _tts_input_metrics(input_chunks)
                self.telemetry.finish_tts_call(
                    call_id,
                    status="success",
                    completed_at=utc_now(),
                    duration_ms=trace.offset_ms() - call_start,
                    input_text=input_text,
                    input_chars=input_chars,
                    input_chunks=len(input_chunks),
                    first_text_ms=first_text_ms,
                    first_audio_ms=first_audio_ms,
                    audio_bytes=audio_bytes,
                    audio_duration_ms=_pcm_duration_ms(
                        audio_bytes,
                        _sample_rate(self.settings.elevenlabs_stream_output_format),
                    ),
                )
            finally:
                trace.event("tts.context_closed", stage="tts", metadata={"context_id": state.context_id})
                await event_queue.put(("complete", "tts"))

        yield self.event(trace, "status", {"state": "thinking"})
        tasks = [asyncio.create_task(produce_llm()), asyncio.create_task(produce_tts())]
        completed = 0
        try:
            while completed < len(tasks):
                kind, value = await event_queue.get()
                if kind == "complete":
                    completed += 1
                else:
                    yield value  # type: ignore[misc]
        except asyncio.CancelledError:
            trace.finish("cancelled", assistant_text=state.assistant_text)
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        state.completed = not llm_failed
        if llm_failed:
            trace.finish("failed", assistant_text=state.assistant_text)
        elif tts_failed:
            trace.finish("partial_failure", assistant_text=state.assistant_text, response_complete=True)
        else:
            trace.finish("success", assistant_text=state.assistant_text, response_complete=True)

    async def _stream_chat_with_audio(
        self,
        trace: TurnTrace,
        *,
        user_text: str,
        model_user_text: str,
        messages: list[dict[str, str]],
        brain_api_key: str | None,
        voice_api_key: str | None,
        voice_id: str | None,
        tts_provider: str | None,
    ) -> AsyncIterator[PipelineEvent]:
        event_queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        assistant_text = ""
        speech_buffer = ""
        llm_failed = False
        tts_failed = False

        async def text_chunks() -> AsyncIterator[str]:
            while True:
                chunk = await text_queue.get()
                if chunk is None:
                    break
                yield chunk

        async def produce_llm() -> None:
            nonlocal assistant_text, speech_buffer, llm_failed
            try:
                async for llm_event in self._stream_llm(trace, messages, api_key=brain_api_key):
                    if llm_event.kind != "delta":
                        continue
                    assistant_text += llm_event.text
                    trace.update_text(assistant_text=assistant_text)
                    await event_queue.put(("event", self.event(trace, "delta", {"text": llm_event.text})))
                    speech_buffer += llm_event.text
                    ready, speech_buffer = pop_ready_speech_chunks(speech_buffer)
                    for chunk in ready:
                        await text_queue.put(chunk)
                        await event_queue.put(("event", self.event(trace, "display", {"text": chunk})))
                final_chunk = speech_buffer.strip()
                if final_chunk:
                    await text_queue.put(final_chunk)
                    await event_queue.put(("event", self.event(trace, "display", {"text": final_chunk})))
                self.sessions.append_turn(trace.session_id, model_user_text, assistant_text)
                await event_queue.put(("event", self.event(trace, "done", {"text": assistant_text})))
            except Exception as exc:
                llm_failed = True
                info = _observed_info(exc, default_stage="llm")
                await event_queue.put(("event", self.event(trace, "error", info.payload(trace.turn_id))))
            finally:
                await text_queue.put(None)
                await event_queue.put(("complete", "llm"))

        async def produce_tts() -> None:
            nonlocal tts_failed
            try:
                tts = self.tts.resolve(tts_provider)
                async for output in self._stream_tts_websocket(
                    trace,
                    text_chunks(),
                    tts=tts,
                    api_key=voice_api_key,
                    voice_id=voice_id,
                ):
                    if output.kind != "audio":
                        continue
                    data = {
                        "audio": base64.b64encode(output.audio).decode("ascii"),
                        "mime_type": tts.stream_media_type,
                        "format": tts.stream_output_format,
                        "sample_rate": tts.stream_sample_rate,
                    }
                    if output.alignment is not None:
                        data["alignment"] = caption_payload(output.alignment)
                    await event_queue.put(
                        (
                            "event",
                            self.event(
                                trace,
                                "audio",
                                data,
                            ),
                        )
                    )
                await event_queue.put(("event", self.event(trace, "audio_done")))
            except Exception as exc:
                tts_failed = True
                info = _observed_info(exc, default_stage="tts")
                await event_queue.put(("event", self.event(trace, "tts_error", info.payload(trace.turn_id))))
            finally:
                await event_queue.put(("complete", "tts"))

        tasks = [asyncio.create_task(produce_llm()), asyncio.create_task(produce_tts())]
        completed = 0
        try:
            while completed < len(tasks):
                kind, value = await event_queue.get()
                if kind == "complete":
                    completed += 1
                else:
                    yield value  # type: ignore[misc]
        except asyncio.CancelledError:
            trace.finish("cancelled", assistant_text=assistant_text)
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if llm_failed:
            trace.finish("failed", assistant_text=assistant_text)
        elif tts_failed:
            trace.finish("partial_failure", assistant_text=assistant_text, response_complete=True)
        else:
            trace.finish("success", assistant_text=assistant_text, response_complete=True)

    async def _stream_llm(
        self,
        trace: TurnTrace,
        messages: list[dict[str, str]],
        *,
        api_key: str | None,
    ) -> AsyncIterator[AIStreamEvent]:
        call_id = uuid.uuid4().hex
        call_start = trace.offset_ms()
        first_token_ms: float | None = None
        response_text = ""
        completion = AIStreamEvent(kind="complete")
        request = AIRequest(
            messages=messages,
            api_key=api_key,
            cache_key=trace.session_id,
            purpose="answer",
        )
        self.telemetry.start_llm_call(
            trace,
            call_id=call_id,
            provider=self.llm.provider,
            model=self.llm.model,
            reasoning_effort=self.llm.reasoning_effort,
            purpose=request.purpose,
            request=self.llm.request_snapshot(request),
        )
        trace.event("llm.requested", stage="llm", metadata={"call_id": call_id})
        try:
            stream = (
                self.llm.stream_text(request)
                if hasattr(self.llm, "stream_text")
                else self.llm.stream(request)
            )
            async for event in stream:
                if event.kind == "delta":
                    if first_token_ms is None:
                        first_token_ms = trace.offset_ms() - call_start
                        trace.event("llm.first_token", stage="llm", metadata={"call_id": call_id})
                    response_text += event.text
                    yield event
                else:
                    completion = event
        except asyncio.CancelledError:
            self.telemetry.finish_llm_call(
                call_id,
                status="cancelled",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                first_token_ms=first_token_ms,
                response_text=response_text,
            )
            raise
        except Exception as exc:
            info = error_info(exc, default_stage="llm")
            self.telemetry.record_error(trace, info, call_id=call_id)
            self.telemetry.finish_llm_call(
                call_id,
                status="failed",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                first_token_ms=first_token_ms,
                response_text=response_text,
                provider_request_id=info.request_id,
                error_id=info.error_id,
            )
            raise ObservedError(info) from exc

        usage = completion.usage
        if not response_text.strip():
            info = error_info(
                GatewayError(
                    stage="llm",
                    provider=self.llm.provider,
                    code="empty_response",
                    public_message="The language model returned an empty response.",
                    technical_message="LLM stream completed without text deltas.",
                    retryable=True,
                    request_id=completion.request_id,
                )
            )
            self.telemetry.record_error(trace, info, call_id=call_id)
            self.telemetry.finish_llm_call(
                call_id,
                status="failed",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                first_token_ms=first_token_ms,
                response_text=response_text,
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
                cached_tokens=usage.cached_tokens if usage else None,
                reasoning_tokens=usage.reasoning_tokens if usage else None,
                cache_status=usage.cache_status if usage else None,
                cost_usd_ticks=usage.cost_usd_ticks if usage else None,
                finish_reason=completion.finish_reason,
                provider_request_id=completion.request_id,
                system_fingerprint=completion.system_fingerprint,
                service_tier=completion.service_tier,
                error_id=info.error_id,
            )
            raise ObservedError(info)
        self.telemetry.finish_llm_call(
            call_id,
            status="success",
            completed_at=utc_now(),
            duration_ms=trace.offset_ms() - call_start,
            first_token_ms=first_token_ms,
            response_text=response_text,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
            cached_tokens=usage.cached_tokens if usage else None,
            reasoning_tokens=usage.reasoning_tokens if usage else None,
            cache_status=usage.cache_status if usage else None,
            cost_usd_ticks=usage.cost_usd_ticks if usage else None,
            finish_reason=completion.finish_reason,
            provider_request_id=completion.request_id,
            system_fingerprint=completion.system_fingerprint,
            service_tier=completion.service_tier,
        )
        trace.event("llm.completed", stage="llm", metadata={"call_id": call_id})
        yield completion

    async def _stream_tts_websocket(
        self,
        trace: TurnTrace,
        chunks: AsyncIterable[str],
        *,
        tts: TTSGateway,
        api_key: str | None,
        voice_id: str | None,
    ) -> AsyncIterator[TTSEvent]:
        call_id = uuid.uuid4().hex
        call_start = trace.offset_ms()
        first_text_ms: float | None = None
        first_audio_ms: float | None = None
        input_chunks: list[str] = []
        audio_bytes = 0
        completion = TTSEvent(kind="complete")
        self.telemetry.start_tts_call(
            trace,
            call_id=call_id,
            provider=tts.provider,
            model=tts.model,
            voice_id=voice_id,
            output_format=tts.stream_output_format,
            sample_rate=tts.stream_sample_rate,
        )

        async def observed_chunks() -> AsyncIterator[str]:
            nonlocal first_text_ms
            async for chunk in chunks:
                chunk = chunk.strip()
                if not chunk:
                    continue
                if first_text_ms is None:
                    first_text_ms = trace.offset_ms() - call_start
                    trace.event("tts.first_text", stage="tts", metadata={"call_id": call_id})
                input_chunks.append(chunk)
                yield chunk

        try:
            async for event in self.tts.stream_websocket(
                observed_chunks(),
                provider=tts.provider,
                api_key=api_key,
                voice_id=voice_id,
            ):
                if event.kind == "audio":
                    if first_audio_ms is None:
                        first_audio_ms = trace.offset_ms() - call_start
                        trace.event("tts.first_audio", stage="tts", metadata={"call_id": call_id})
                    audio_bytes += len(event.audio)
                    yield event
                else:
                    completion = event
        except asyncio.CancelledError:
            input_text, input_chars = _tts_input_metrics(input_chunks)
            self.telemetry.finish_tts_call(
                call_id,
                status="cancelled",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                input_text=input_text,
                input_chars=input_chars,
                input_chunks=len(input_chunks),
                first_text_ms=first_text_ms,
                first_audio_ms=first_audio_ms,
                audio_bytes=audio_bytes,
                audio_duration_ms=_pcm_duration_ms(audio_bytes, tts.stream_sample_rate),
            )
            raise
        except Exception as exc:
            info = error_info(exc, default_stage="tts")
            self.telemetry.record_error(trace, info, call_id=call_id)
            input_text, input_chars = _tts_input_metrics(input_chunks)
            self.telemetry.finish_tts_call(
                call_id,
                status="failed",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                input_text=input_text,
                input_chars=input_chars,
                input_chunks=len(input_chunks),
                first_text_ms=first_text_ms,
                first_audio_ms=first_audio_ms,
                audio_bytes=audio_bytes,
                audio_duration_ms=_pcm_duration_ms(audio_bytes, tts.stream_sample_rate),
                error_id=info.error_id,
            )
            raise ObservedError(info) from exc

        input_text, input_chars = _tts_input_metrics(input_chunks)
        if audio_bytes == 0:
            info = error_info(
                GatewayError(
                    stage="tts",
                    provider=tts.provider,
                    code="empty_audio",
                    public_message="Voice service returned no audio.",
                    technical_message="WebSocket TTS stream completed without audio bytes.",
                    retryable=True,
                    request_id=completion.request_id,
                )
            )
            self.telemetry.record_error(trace, info, call_id=call_id)
            self.telemetry.finish_tts_call(
                call_id,
                status="failed",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                input_text=input_text,
                input_chars=input_chars,
                input_chunks=len(input_chunks),
                first_text_ms=first_text_ms,
                first_audio_ms=None,
                audio_bytes=0,
                character_cost=completion.character_cost,
                provider_request_id=completion.request_id,
                trace_id=completion.trace_id,
                error_id=info.error_id,
            )
            raise ObservedError(info)
        self.telemetry.finish_tts_call(
            call_id,
            status="success",
            completed_at=utc_now(),
            duration_ms=trace.offset_ms() - call_start,
            input_text=input_text,
            input_chars=input_chars,
            input_chunks=len(input_chunks),
            first_text_ms=first_text_ms,
            first_audio_ms=first_audio_ms,
            audio_bytes=audio_bytes,
            audio_duration_ms=_pcm_duration_ms(audio_bytes, tts.stream_sample_rate),
            character_cost=completion.character_cost,
            provider_request_id=completion.request_id,
            trace_id=completion.trace_id,
        )

    async def _transcribe_upload(
        self,
        trace: TurnTrace,
        *,
        filename: str,
        content_type: str | None,
        data: bytes,
        api_key: str | None,
    ) -> STTResult:
        call_id = uuid.uuid4().hex
        call_start = trace.offset_ms()
        self.telemetry.start_stt_call(
            trace,
            call_id=call_id,
            provider=self.stt.provider,
            model=self.stt.upload_model,
            sample_rate=None,
        )
        try:
            result = await self.stt.transcribe_upload(
                filename=filename,
                content_type=content_type,
                data=data,
                api_key=api_key,
            )
        except asyncio.CancelledError:
            self.telemetry.finish_stt_call(
                call_id,
                status="cancelled",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                audio_bytes=len(data),
            )
            raise
        except Exception as exc:
            info = error_info(exc, default_stage="stt")
            self.telemetry.record_error(trace, info, call_id=call_id)
            self.telemetry.finish_stt_call(
                call_id,
                status="failed",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                audio_bytes=len(data),
                error_id=info.error_id,
            )
            raise ObservedError(info) from exc
        commit_ms = trace.offset_ms() - call_start
        trace.event("stt.committed", stage="stt", metadata={"call_id": call_id})
        self.telemetry.finish_stt_call(
            call_id,
            status="success",
            completed_at=utc_now(),
            duration_ms=commit_ms,
            audio_bytes=len(data),
            committed_count=1,
            commit_latency_ms=commit_ms,
            transcript_text=result.text,
            language_code=result.language_code,
            provider_request_id=result.request_id,
        )
        return result

    async def _transcribe_realtime(
        self,
        trace: TurnTrace,
        audio_chunks: AsyncIterable[bytes],
        *,
        sample_rate: int,
        api_key: str | None,
        vad_threshold: float | None = None,
        vad_silence_threshold_secs: float | None = None,
    ) -> AsyncIterator[STTEvent]:
        call_id = uuid.uuid4().hex
        call_start = trace.offset_ms()
        audio_bytes = 0
        partial_count = 0
        committed_count = 0
        first_partial_ms: float | None = None
        transcript_parts: list[str] = []
        request_id: str | None = None
        language_code: str | None = None
        self.telemetry.start_stt_call(
            trace,
            call_id=call_id,
            provider=self.stt.provider,
            model=self.stt.realtime_model,
            sample_rate=sample_rate,
        )

        async def observed_audio() -> AsyncIterator[bytes]:
            nonlocal audio_bytes
            async for chunk in audio_chunks:
                audio_bytes += len(chunk)
                yield chunk

        try:
            async for event in self.stt.stream_realtime(
                observed_audio(),
                sample_rate=sample_rate,
                api_key=api_key,
                vad_threshold=vad_threshold,
                vad_silence_threshold_secs=vad_silence_threshold_secs,
            ):
                request_id = event.request_id or request_id
                language_code = event.language_code or language_code
                if event.kind == "partial":
                    partial_count += 1
                    if first_partial_ms is None:
                        first_partial_ms = trace.offset_ms() - call_start
                        trace.event("stt.first_partial", stage="stt", metadata={"call_id": call_id})
                elif event.kind == "session_started":
                    trace.event("stt.connected", stage="stt", metadata={"call_id": call_id})
                elif event.kind == "committed":
                    committed_count += 1
                    if event.text:
                        transcript_parts.append(event.text)
                    trace.event("stt.committed", stage="stt", metadata={"call_id": call_id})
                yield event
        except asyncio.CancelledError:
            self.telemetry.finish_stt_call(
                call_id,
                status="cancelled",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                audio_bytes=audio_bytes,
                audio_duration_ms=_pcm_duration_ms(audio_bytes, sample_rate),
                partial_count=partial_count,
                committed_count=committed_count,
                first_partial_ms=first_partial_ms,
                transcript_text=" ".join(transcript_parts),
                provider_request_id=request_id,
            )
            raise
        except Exception as exc:
            info = error_info(exc, default_stage="stt")
            self.telemetry.record_error(trace, info, call_id=call_id)
            self.telemetry.finish_stt_call(
                call_id,
                status="failed",
                completed_at=utc_now(),
                duration_ms=trace.offset_ms() - call_start,
                audio_bytes=audio_bytes,
                audio_duration_ms=_pcm_duration_ms(audio_bytes, sample_rate),
                partial_count=partial_count,
                committed_count=committed_count,
                first_partial_ms=first_partial_ms,
                transcript_text=" ".join(transcript_parts),
                provider_request_id=request_id,
                error_id=info.error_id,
            )
            raise ObservedError(info) from exc

        commit_latency = trace.offset_ms() - call_start
        self.telemetry.finish_stt_call(
            call_id,
            status="success",
            completed_at=utc_now(),
            duration_ms=commit_latency,
            audio_bytes=audio_bytes,
            audio_duration_ms=_pcm_duration_ms(audio_bytes, sample_rate),
            partial_count=partial_count,
            committed_count=committed_count,
            first_partial_ms=first_partial_ms,
            commit_latency_ms=commit_latency,
            transcript_text=" ".join(transcript_parts),
            language_code=language_code,
            provider_request_id=request_id,
        )

    def _validate_user_text(self, text: str) -> None:
        if not text.strip():
            raise GatewayError(
                stage="stt",
                provider=self.stt.provider,
                code="no_speech",
                public_message="没有识别到语音。",
                technical_message="Transcription completed without text.",
            )
        if self.settings.max_chat_chars > 0 and len(text) > self.settings.max_chat_chars:
            raise GatewayError(
                stage="stt",
                provider=self.stt.provider,
                code="transcription_too_long",
                public_message="Transcription is too long.",
                technical_message=f"Transcription exceeded {self.settings.max_chat_chars} characters.",
            )


def _pcm_duration_ms(audio_bytes: int, sample_rate: int | None) -> float | None:
    if not sample_rate or sample_rate <= 0:
        return None
    return audio_bytes / (sample_rate * 2) * 1000


def _tts_input_metrics(chunks: list[str]) -> tuple[str, int]:
    return " ".join(chunks), sum(len(chunk) + 1 for chunk in chunks)


def _sample_rate(output_format: str) -> int | None:
    if not output_format.startswith("pcm_"):
        return None
    try:
        return int(output_format.split("_", 1)[1])
    except (IndexError, ValueError):
        return None


def resolve_response_language(
    mode: str | None,
    detected_language: str | None,
    previous_language: str | None = None,
) -> str:
    selected = str(mode or "auto").strip().lower()
    if selected in {"en", "zh"}:
        return selected
    detected = str(detected_language or "").strip().lower()
    if detected.startswith("en") or detected == "eng":
        return "en"
    if detected.startswith("zh") or detected in {"cmn", "yue"}:
        return "zh"
    previous = str(previous_language or "").strip().lower()
    return previous if previous in {"en", "zh"} else "en"


def _observed_info(exc: Exception, *, default_stage: str) -> ErrorInfo:
    if isinstance(exc, ObservedError):
        return exc.info
    return error_info(exc, default_stage=default_stage)
