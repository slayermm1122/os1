from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.errors import GatewayError, error_info
from ..core.orchestrator import (
    LiveTurnState,
    PipelineEvent,
    resolve_response_language,
)
from ..core.security import is_allowed_websocket
from ..services import ApplicationServices
from ..telemetry.sqlite_store import utc_now
from .protocol import websocket_message
from .routes import resolve_voice_id


SUPPORTED_SAMPLE_RATES = {8000, 16000, 22050, 24000, 44100, 48000}
logger = logging.getLogger("os1.realtime")


def create_realtime_router(services: ApplicationServices) -> APIRouter:
    router = APIRouter()

    @router.websocket("/api/realtime/turn")
    async def realtime_turn(websocket: WebSocket) -> None:
        if services.settings.enforce_local_access and not is_allowed_websocket(
            websocket.client.host if websocket.client else None,
            websocket.headers.get("host"),
            websocket.headers.get("origin"),
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        client_host = websocket.client.host if websocket.client else "unknown"
        if not services.rate_limiter.consume(client_host):
            await _send(
                websocket,
                PipelineEvent(
                    event="error",
                    data={"message": "Too many requests. Please wait before trying again."},
                ),
            )
            await websocket.close(code=1008)
            return

        try:
            init_raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            init = json.loads(init_raw)
            if not isinstance(init, dict):
                raise ValueError("Realtime start message must be a JSON object.")
        except Exception:
            await _send(websocket, PipelineEvent("error", {"message": "Invalid realtime start message."}))
            await websocket.close(code=1003)
            return
        if init.get("type") != "start":
            await _send(websocket, PipelineEvent("error", {"message": "Invalid realtime start message."}))
            await websocket.close(code=1003)
            return

        session_id = str(init.get("session_id") or "")[:128] or uuid.uuid4().hex
        try:
            sample_rate = int(init.get("sample_rate") or 16000)
        except (TypeError, ValueError):
            sample_rate = 0
        if sample_rate not in SUPPORTED_SAMPLE_RATES:
            await _send(websocket, PipelineEvent("error", {"message": "Unsupported audio sample rate."}))
            await websocket.close(code=1003)
            return

        trace = services.orchestrator.new_trace(session_id=session_id, kind="voice_realtime")
        audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=24)
        send_lock = asyncio.Lock()
        playback_received = asyncio.Event()
        playback_failure_received = asyncio.Event()
        stop_lock = asyncio.Lock()
        stopped = False
        total_audio_bytes = 0
        sent_audio = False
        transport_failed = False

        async def audio_chunks() -> AsyncIterator[bytes]:
            while True:
                chunk = await audio_queue.get()
                if chunk is None:
                    break
                yield chunk

        async def send_event(event: PipelineEvent) -> None:
            nonlocal sent_audio
            if event.event == "audio":
                sent_audio = True
            if event.event == "recording_stopped":
                # VAD or STT finished — end the mic stream so the audio queue cannot block.
                await stop_input("stt_complete")
            async with send_lock:
                await _send(websocket, event)

        async def stop_input(reason: str) -> bool:
            nonlocal stopped
            async with stop_lock:
                if stopped:
                    return False
                stopped = True
                trace.event(
                    "recording.stop_received",
                    stage="stt",
                    metadata={"reason": reason},
                )
                # Never block the pipeline on a full audio queue after VAD ends.
                while True:
                    try:
                        audio_queue.put_nowait(None)
                        break
                    except asyncio.QueueFull:
                        try:
                            audio_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                return True

        async def run_pipeline() -> None:
            async for event in services.orchestrator.stream_realtime_turn(
                trace,
                audio_chunks(),
                sample_rate=sample_rate,
                brain_api_key=None,
                voice_api_key=None,
                voice_id=resolve_voice_id(services.orchestrator.tts, None, None, None),
                tts_provider=None,
                stt_api_key=None,
                tts_api_key=None,
                vad_threshold=services.settings.elevenlabs_stt_vad_threshold,
                vad_silence_threshold_secs=(
                    services.settings.elevenlabs_stt_vad_silence_threshold_secs
                ),
                assistant_name=services.settings.assistant_name,
                assistant_name_pronunciation=services.settings.assistant_name_pronunciation,
                user_name=services.settings.user_name,
                user_name_pronunciation=services.settings.user_name_pronunciation,
                assistant_persona=services.settings.assistant_persona,
            ):
                await send_event(event)

        async def receive_client() -> None:
            nonlocal stopped, total_audio_bytes, transport_failed
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect()
                audio = message.get("bytes")
                if audio is not None:
                    if stopped:
                        continue
                    next_total = total_audio_bytes + len(audio)
                    if next_total > services.settings.max_upload_bytes:
                        error = GatewayError(
                            stage="stt",
                            provider=services.orchestrator.stt.provider,
                            code="recording_too_large",
                            public_message="Recording is too large.",
                        )
                        info = error_info(error)
                        services.telemetry.record_error(trace, info)
                        transport_failed = True
                        trace.require_terminal_status("failed")
                        await audio_queue.put(None)
                        await send_event(PipelineEvent("error", info.payload(trace.turn_id)))
                        return
                    total_audio_bytes = next_total
                    await audio_queue.put(audio)
                    continue
                raw = message.get("text")
                if raw is None:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "stop" and not stopped:
                    await stop_input("client")
                elif payload.get("type") == "client_event":
                    if (
                        sent_audio
                        and not playback_received.is_set()
                        and _record_client_event(trace, payload)
                    ):
                        playback_received.set()
                    elif (
                        sent_audio
                        and not playback_failure_received.is_set()
                        and _record_browser_error(services, trace, payload)
                    ):
                        playback_failure_received.set()
                        playback_received.set()

        pipeline_task = asyncio.create_task(run_pipeline(), name=f"turn-{trace.turn_id}")
        receive_task = asyncio.create_task(receive_client(), name=f"client-{trace.turn_id}")
        disconnected = False
        try:
            done, _ = await asyncio.wait(
                {pipeline_task, receive_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receive_task in done:
                exc = receive_task.exception()
                if exc:
                    raise exc
            if pipeline_task in done:
                exc = pipeline_task.exception()
                if exc:
                    raise exc
                await stop_input("pipeline_complete")
                if sent_audio:
                    if not playback_received.is_set():
                        try:
                            await asyncio.wait_for(playback_received.wait(), timeout=1.0)
                        except TimeoutError:
                            pass
                    if playback_received.is_set() and not playback_failure_received.is_set():
                        try:
                            await asyncio.wait_for(playback_failure_received.wait(), timeout=0.1)
                        except TimeoutError:
                            pass
        except asyncio.CancelledError:
            disconnected = True
        except WebSocketDisconnect:
            disconnected = True
        except Exception as exc:
            info = error_info(exc)
            services.telemetry.record_error(trace, info)
            transport_failed = True
            trace.require_terminal_status("failed")
            try:
                await send_event(PipelineEvent("error", info.payload(trace.turn_id)))
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            if not stopped:
                try:
                    audio_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            for task in (pipeline_task, receive_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                pipeline_task,
                receive_task,
                return_exceptions=True,
            )
            if disconnected and not trace.finished:
                trace.finish("cancelled")
            elif transport_failed and not trace.finished:
                trace.finish("failed")
            try:
                await websocket.close()
            except RuntimeError:
                pass

    @router.websocket("/api/realtime/session")
    async def realtime_session(websocket: WebSocket) -> None:
        if services.settings.enforce_local_access and not is_allowed_websocket(
            websocket.client.host if websocket.client else None,
            websocket.headers.get("host"),
            websocket.headers.get("origin"),
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        client_host = websocket.client.host if websocket.client else "unknown"
        if not services.rate_limiter.consume(client_host):
            await _send(websocket, PipelineEvent("error", {"message": "Too many requests."}))
            await websocket.close(code=1008)
            return
        try:
            init = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=10))
            if not isinstance(init, dict) or init.get("type") != "session_start":
                raise ValueError
            session_id = str(init.get("session_id") or uuid.uuid4().hex)[:128]
            sample_rate = int(init.get("sample_rate") or 16000)
            if sample_rate not in SUPPORTED_SAMPLE_RATES:
                raise ValueError
            session_persona = {
                "assistant_name": services.settings.assistant_name,
                "assistant_name_pronunciation": services.settings.assistant_name_pronunciation,
                "user_name": services.settings.user_name,
                "user_name_pronunciation": services.settings.user_name_pronunciation,
                "persona": services.settings.assistant_persona,
            }
        except Exception:
            await _send(
                websocket,
                PipelineEvent("error", {"message": "Invalid live session start message."}),
            )
            await websocket.close(code=1003)
            return

        tts_gateway = services.orchestrator.tts.resolve()
        create_live_session = getattr(tts_gateway, "create_live_session", None)
        if create_live_session is None:
            await _send(
                websocket,
                PipelineEvent("error", {"message": "The selected TTS provider does not support Live."}),
            )
            await websocket.close(code=1011)
            return

        live_tts = create_live_session(services.settings.elevenlabs_voice_id)
        session_key = uuid.uuid4().hex
        send_lock = asyncio.Lock()
        history_lock = asyncio.Lock()
        audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        close_event = asyncio.Event()
        listening_event = asyncio.Event()
        pending_settings: set[str] = set()
        interruption_waiters: dict[str, asyncio.Future[str]] = {}
        background_turn_tasks: set[asyncio.Task[None]] = set()
        active_turn_task: asyncio.Task[None] | None = None
        active_state: LiveTurnState | None = None
        last_language = "en"
        last_activity = asyncio.get_running_loop().time()
        stt_task: asyncio.Task[None] | None = None
        stt_utterance_active = False
        utterance_started_at: float | None = None
        utterance_first_partial_at: float | None = None
        utterance_audio_bytes = 0
        utterance_partial_count = 0
        background_since: float | None = None
        disconnected = False

        async def send_event(event: PipelineEvent) -> None:
            async with send_lock:
                await _send(websocket, event)

        async def notify_setting(kind: str) -> None:
            nonlocal stt_task
            pending_settings.add(kind)
            if (
                kind in {"language", "keyterms"}
                and listening_event.is_set()
                and not stt_utterance_active
                and stt_task is not None
                and not stt_task.done()
            ):
                old_task = stt_task
                old_task.cancel()
                await asyncio.gather(old_task, return_exceptions=True)
                pending_settings.discard(kind)
                if listening_event.is_set() and not close_event.is_set():
                    stt_task = asyncio.create_task(
                        run_stt(),
                        name=f"live-stt-{session_id}",
                    )

        async def close_from_registry() -> None:
            close_event.set()

        async def commit_history(state: LiveTurnState, spoken_text: str | None) -> None:
            async with history_lock:
                if state.history_committed or not state.model_user_text:
                    return
                assistant_text = (
                    state.assistant_text
                    if spoken_text is None
                    else _safe_spoken_prefix(state.assistant_text, spoken_text)
                )
                if spoken_text is not None and state.playback_started and not assistant_text:
                    assistant_text = "(interrupted by user)"
                services.orchestrator.sessions.append_exchange(
                    session_id,
                    state.model_user_text,
                    assistant_text or None,
                )
                state.history_committed = True

        async def commit_record(state: LiveTurnState) -> None:
            if state.record_committed or not state.completed or services.chat_history is None:
                return
            await asyncio.to_thread(
                services.chat_history.append_turn,
                session_id=session_id,
                turn_id=state.context_id,
                user_text=state.user_text,
                assistant_text=state.assistant_text,
                interrupted=state.interrupted,
                playback_started=state.playback_started,
                spoken_text=state.spoken_text,
            )
            state.record_committed = True
            await send_event(PipelineEvent("history_updated", {"session_id": session_id}))

        async def interrupt_active() -> None:
            nonlocal active_turn_task, active_state, last_activity
            state = active_state
            if state is None or state.history_committed:
                return
            state.interrupted = True
            await live_tts.close_context(state.context_id)
            if state.trace is not None:
                state.trace.event(
                    "conversation.barge_in",
                    stage="turn",
                    metadata={"context_id": state.context_id},
                )
            waiter = asyncio.get_running_loop().create_future()
            interruption_waiters[state.context_id] = waiter
            await send_event(
                PipelineEvent(
                    "barge_in",
                    {
                        "turn_id": state.context_id,
                        "session_id": session_id,
                        "context_id": state.context_id,
                    },
                )
            )
            try:
                spoken = await asyncio.wait_for(waiter, timeout=0.4)
            except TimeoutError:
                spoken = ""
            interruption_waiters.pop(state.context_id, None)
            state.spoken_text = spoken
            state.playback_started = state.playback_started or bool(spoken)
            if state.playback_started:
                await commit_history(state, spoken)
            else:
                state.history_committed = True
            last_activity = asyncio.get_running_loop().time()
            if active_turn_task is not None and not active_turn_task.done():
                background_turn_tasks.add(active_turn_task)
                active_turn_task.add_done_callback(background_turn_tasks.discard)
            elif state.completed:
                await commit_record(state)
            active_turn_task = None
            active_state = None

        async def apply_pending_tts_settings() -> None:
            nonlocal live_tts
            if not pending_settings.intersection({"voice", "tts_reconnect"}):
                pending_settings.discard("dictionary")
                return
            pending_settings.discard("voice")
            pending_settings.discard("tts_reconnect")
            pending_settings.discard("dictionary")
            await live_tts.close()
            live_tts = create_live_session(services.settings.elevenlabs_voice_id)
            await live_tts.connect()

        async def run_turn(state: LiveTurnState, language: str, trace) -> None:
            nonlocal last_activity
            try:
                async for event in services.orchestrator.stream_live_chat(
                    trace,
                    state=state,
                    live_tts=live_tts,
                    response_language=language,
                    persona_snapshot=session_persona,
                ):
                    if event.event in {"delta", "display", "audio"}:
                        last_activity = asyncio.get_running_loop().time()
                    if event.event == "tts_error":
                        pending_settings.add("tts_reconnect")
                    await send_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Live turn failed", exc_info=exc)
                info = error_info(exc)
                if not trace.finished:
                    trace.finish("failed", assistant_text=state.assistant_text)
                await send_event(PipelineEvent("error", info.payload(trace.turn_id)))
            finally:
                if state.interrupted or state.history_committed:
                    await commit_record(state)

        async def start_turn(
            user_text: str,
            detected_language: str | None,
            *,
            stt_duration_ms: float,
            stt_audio_bytes: int,
            stt_partial_count: int,
            stt_first_partial_ms: float | None,
        ) -> None:
            nonlocal active_turn_task, active_state, last_language, last_activity
            if active_state is not None and not active_state.history_committed:
                await interrupt_active()
            await apply_pending_tts_settings()
            language = resolve_response_language(
                services.settings.assistant_response_language,
                detected_language,
                last_language,
            )
            last_language = language
            state = LiveTurnState(user_text=user_text, response_language=language)
            trace = services.orchestrator.new_trace(session_id=session_id, kind="voice_live")
            state.context_id = trace.turn_id
            state.trace = trace
            if services.chat_history is not None:
                await asyncio.to_thread(
                    services.chat_history.append_user,
                    session_id=session_id,
                    turn_id=state.context_id,
                    user_text=state.user_text,
                )
                await send_event(
                    PipelineEvent(
                        "history_updated",
                        {"session_id": session_id, "turn_id": state.context_id, "phase": "user"},
                    )
                )
            stt_call_id = uuid.uuid4().hex
            services.telemetry.start_stt_call(
                trace,
                call_id=stt_call_id,
                provider=services.orchestrator.stt.provider,
                model=services.orchestrator.stt.realtime_model,
                sample_rate=sample_rate,
            )
            trace.event(
                "stt.connection_reused",
                stage="stt",
                metadata={"call_id": stt_call_id},
            )
            trace.event("stt.committed", stage="stt", metadata={"call_id": stt_call_id})
            services.telemetry.finish_stt_call(
                stt_call_id,
                status="success",
                completed_at=utc_now(),
                duration_ms=stt_duration_ms,
                audio_bytes=stt_audio_bytes,
                audio_duration_ms=stt_audio_bytes / (sample_rate * 2) * 1000,
                partial_count=stt_partial_count,
                committed_count=1,
                first_partial_ms=stt_first_partial_ms,
                commit_latency_ms=stt_duration_ms,
                transcript_text=user_text,
                language_code=detected_language,
            )
            active_state = state
            active_turn_task = asyncio.create_task(
                run_turn(state, language, trace),
                name=f"live-turn-{session_id}",
            )
            last_activity = asyncio.get_running_loop().time()

        async def audio_chunks() -> AsyncIterator[bytes]:
            while True:
                chunk = await audio_queue.get()
                if chunk is None:
                    break
                yield chunk

        async def run_stt() -> None:
            nonlocal last_activity, stt_utterance_active
            nonlocal utterance_started_at, utterance_first_partial_at
            nonlocal utterance_audio_bytes, utterance_partial_count
            retries = 0
            while not close_event.is_set() and listening_event.is_set():
                detect_language = services.settings.assistant_response_language == "auto"
                try:
                    async for event in services.orchestrator.stt.stream_realtime(
                        audio_chunks(),
                        sample_rate=sample_rate,
                        api_key=None,
                        vad_threshold=services.settings.elevenlabs_stt_vad_threshold,
                        vad_silence_threshold_secs=(
                            services.settings.elevenlabs_stt_vad_silence_threshold_secs
                        ),
                        continuous=True,
                        detect_language=detect_language,
                        filter_background_audio=True,
                        keyterms=services.settings.elevenlabs_stt_keyterms,
                    ):
                        if event.kind == "session_started":
                            await send_event(
                                PipelineEvent(
                                    "stt_ready",
                                    {"session_id": session_id, "state": "listening"},
                                )
                            )
                        elif event.kind == "partial":
                            partial = event.text.strip()
                            if not partial:
                                continue
                            stt_utterance_active = True
                            utterance_partial_count += 1
                            if utterance_first_partial_at is None:
                                utterance_first_partial_at = asyncio.get_running_loop().time()
                            last_activity = asyncio.get_running_loop().time()
                            await send_event(
                                PipelineEvent(
                                    "transcript_partial",
                                    {"session_id": session_id, "text": partial},
                                )
                            )
                            if active_state is not None and not active_state.history_committed:
                                await interrupt_active()
                        elif event.kind == "committed" and event.text.strip():
                            retries = 0
                            text_value = event.text.strip()
                            stt_utterance_active = False
                            now = asyncio.get_running_loop().time()
                            started_at = utterance_started_at or now
                            duration_ms = max(0.0, (now - started_at) * 1000)
                            first_partial_ms = (
                                max(0.0, (utterance_first_partial_at - started_at) * 1000)
                                if utterance_first_partial_at is not None
                                else None
                            )
                            audio_bytes = utterance_audio_bytes
                            partial_count = utterance_partial_count
                            utterance_started_at = None
                            utterance_first_partial_at = None
                            utterance_audio_bytes = 0
                            utterance_partial_count = 0
                            last_activity = asyncio.get_running_loop().time()
                            await send_event(
                                PipelineEvent(
                                    "transcript",
                                    {
                                        "session_id": session_id,
                                        "text": text_value,
                                        "language_code": event.language_code or "",
                                    },
                                )
                            )
                            await start_turn(
                                text_value,
                                event.language_code,
                                stt_duration_ms=duration_ms,
                                stt_audio_bytes=audio_bytes,
                                stt_partial_count=partial_count,
                                stt_first_partial_ms=first_partial_ms,
                            )
                            if pending_settings.intersection({"language", "keyterms"}):
                                pending_settings.difference_update({"language", "keyterms"})
                                break
                    if close_event.is_set() or not listening_event.is_set():
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    retries += 1
                    if retries <= 1 and not close_event.is_set():
                        await send_event(
                            PipelineEvent(
                                "status",
                                {"session_id": session_id, "state": "reconnecting_stt"},
                            )
                        )
                        continue
                    info = error_info(exc, default_stage="stt")
                    await send_event(PipelineEvent("error", info.payload()))
                    close_event.set()
                    return

        async def receive_client() -> None:
            nonlocal stt_task, last_activity, disconnected, active_state, background_since
            nonlocal utterance_started_at, utterance_audio_bytes
            try:
                while not close_event.is_set():
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        disconnected = True
                        close_event.set()
                        return
                    audio = message.get("bytes")
                    if audio is not None:
                        if listening_event.is_set():
                            if utterance_started_at is None:
                                utterance_started_at = asyncio.get_running_loop().time()
                            utterance_audio_bytes += len(audio)
                            if audio_queue.full():
                                try:
                                    audio_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                            await audio_queue.put(audio)
                        continue
                    raw = message.get("text")
                    if raw is None:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    kind = payload.get("type")
                    if kind == "listen_start" and not listening_event.is_set():
                        listening_event.set()
                        last_activity = asyncio.get_running_loop().time()
                        stt_task = asyncio.create_task(run_stt(), name=f"live-stt-{session_id}")
                        await send_event(
                            PipelineEvent(
                                "listening_started",
                                {"session_id": session_id, "state": "connecting_stt"},
                            )
                        )
                    elif kind == "close_session":
                        close_event.set()
                        return
                    elif kind == "client_event":
                        name = str(payload.get("name") or "")
                        if name == "browser.background":
                            background_since = asyncio.get_running_loop().time()
                        elif name == "browser.foreground":
                            background_since = None
                        if name in {"browser.playback_started", "browser.playback_ended"}:
                            last_activity = asyncio.get_running_loop().time()
                        if name == "browser.playback_started" and active_state is not None:
                            turn_id = str(payload.get("turn_id") or "")
                            if turn_id == active_state.context_id:
                                active_state.playback_started = True
                        if name == "browser.playback_ended" and active_state is not None:
                            turn_id = str(payload.get("turn_id") or "")
                            if turn_id == active_state.context_id:
                                await commit_history(active_state, None)
                                await commit_record(active_state)
                                active_state = None
                        elif name == "browser.playback_interrupted":
                            turn_id = str(payload.get("turn_id") or "")
                            waiter = interruption_waiters.get(turn_id)
                            if waiter is not None and not waiter.done():
                                waiter.set_result(str(payload.get("spoken_text") or ""))
            except WebSocketDisconnect:
                disconnected = True
                close_event.set()

        async def idle_watchdog() -> None:
            nonlocal last_activity
            while not close_event.is_set():
                await asyncio.sleep(5)
                if (
                    background_since is not None
                    and asyncio.get_running_loop().time() - background_since >= 180
                ):
                    await send_event(
                        PipelineEvent(
                            "session_idle",
                            {"session_id": session_id, "reason": "background_timeout"},
                        )
                    )
                    close_event.set()
                    return
                if asyncio.get_running_loop().time() - last_activity < 180:
                    continue
                await send_event(
                    PipelineEvent(
                        "session_idle",
                        {"session_id": session_id, "reason": "inactivity"},
                    )
                )
                close_event.set()
                return

        try:
            if services.live_sessions is not None:
                await services.live_sessions.register(
                    session_key,
                    notify_setting,
                    close_from_registry,
                )
            await live_tts.connect()
            await send_event(
                PipelineEvent(
                    "live_ready",
                    {
                        "session_id": session_id,
                        "state": "preconnected",
                        "voice_id": services.settings.elevenlabs_voice_id,
                        "persona": session_persona,
                    },
                )
            )
            receive_task = asyncio.create_task(receive_client(), name=f"live-client-{session_id}")
            idle_task = asyncio.create_task(idle_watchdog(), name=f"live-idle-{session_id}")
            await close_event.wait()
        except Exception as exc:
            info = error_info(exc, default_stage="tts")
            try:
                await send_event(PipelineEvent("error", info.payload()))
            except Exception:
                pass
        finally:
            listening_event.clear()
            try:
                audio_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            for task in (
                active_turn_task,
                stt_task,
                locals().get("receive_task"),
                locals().get("idle_task"),
            ):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (
                        active_turn_task,
                        stt_task,
                        locals().get("receive_task"),
                        locals().get("idle_task"),
                    )
                    if task is not None
                ),
                return_exceptions=True,
            )
            if background_turn_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*background_turn_tasks, return_exceptions=True),
                        timeout=services.settings.upstream_stream_timeout_seconds,
                    )
                except TimeoutError:
                    for task in background_turn_tasks:
                        task.cancel()
            if active_state is not None and not active_state.history_committed:
                await commit_history(active_state, "")
                await commit_record(active_state)
            await live_tts.close()
            if services.live_sessions is not None:
                await services.live_sessions.unregister(session_key)
            if not disconnected:
                try:
                    await send_event(
                        PipelineEvent(
                            "session_closed",
                            {"session_id": session_id, "state": "disconnected"},
                        )
                    )
                    await websocket.close()
                except Exception:
                    pass

    return router


def _record_client_event(trace, payload: dict[str, object]) -> bool:
    if payload.get("name") != "browser.playback_started":
        return False
    if payload.get("turn_id") != trace.turn_id:
        return False
    elapsed = payload.get("client_elapsed_ms")
    if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed):
        return False
    if elapsed < 0 or elapsed > 600_000:
        return False
    trace.event(
        "browser.playback_started",
        stage="browser",
        metadata={"client_elapsed_ms": round(float(elapsed), 3)},
    )
    return True


def _record_browser_error(services: ApplicationServices, trace, payload: dict[str, object]) -> bool:
    if payload.get("name") != "browser.playback_failed":
        return False
    if payload.get("turn_id") != trace.turn_id:
        return False
    detail = str(payload.get("message") or "Browser audio playback failed.")[:500]
    info = error_info(
        GatewayError(
            stage="browser",
            provider="browser_audio",
            code="audio_playback_failed",
            public_message="Browser audio playback failed.",
            technical_message=detail,
            retryable=True,
        )
    )
    services.telemetry.record_error(trace, info)
    trace.mark_partial_failure()
    return True


async def _send(websocket: WebSocket, event: PipelineEvent) -> None:
    await websocket.send_text(websocket_message(event))


def _safe_spoken_prefix(generated: str, spoken: str) -> str:
    candidate = str(spoken or "").strip()
    if not candidate:
        return ""
    if generated.startswith(candidate):
        return candidate
    length = 0
    for expected, actual in zip(generated, candidate):
        if expected != actual:
            break
        length += 1
    return generated[:length].rstrip()
