from __future__ import annotations

import asyncio
import json
import math
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.errors import GatewayError, error_info
from ..core.orchestrator import PipelineEvent
from ..core.security import is_allowed_websocket
from ..services import ApplicationServices
from .protocol import websocket_message
from .routes import resolve_voice_id


SUPPORTED_SAMPLE_RATES = {8000, 16000, 22050, 24000, 44100, 48000}


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
        stt_ready = asyncio.Event()
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
            async with send_lock:
                await _send(websocket, event)
            if event.event == "stt_ready":
                stt_ready.set()

        async def stop_input(reason: str) -> bool:
            nonlocal stopped
            async with stop_lock:
                if stopped:
                    return False
                stopped = True
                if reason == "server_limit":
                    trace.event("recording.limit_reached", stage="stt")
                trace.event(
                    "recording.stop_received",
                    stage="stt",
                    metadata={"reason": reason},
                )
                await audio_queue.put(None)
                if reason == "server_limit":
                    await send_event(
                        services.orchestrator.event(
                            trace,
                            "recording_stopped",
                            {"reason": "limit"},
                        )
                    )
                await send_event(
                    services.orchestrator.event(trace, "status", {"state": "transcribing"})
                )
                return True

        async def enforce_recording_limit() -> None:
            await stt_ready.wait()
            await asyncio.sleep(max(services.settings.max_recording_seconds, 0.1))
            await stop_input("server_limit")

        async def run_pipeline() -> None:
            async for event in services.orchestrator.stream_realtime_turn(
                trace,
                audio_chunks(),
                sample_rate=sample_rate,
                brain_api_key=str(init.get("brain_api_key") or ""),
                voice_api_key=str(init.get("voice_api_key") or ""),
                voice_id=resolve_voice_id(
                    services.settings,
                    str(init.get("voice_id") or ""),
                    str(init.get("voice_gender") or ""),
                ),
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
                    recording_byte_limit = int(
                        sample_rate * 2 * services.settings.max_recording_seconds
                    )
                    if recording_byte_limit > 0 and next_total > recording_byte_limit:
                        remaining = max(0, recording_byte_limit - total_audio_bytes)
                        if remaining:
                            remaining -= remaining % 2
                            if remaining:
                                await audio_queue.put(audio[:remaining])
                                total_audio_bytes += remaining
                        await stop_input("server_limit")
                        continue
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
        limit_task = asyncio.create_task(
            enforce_recording_limit(),
            name=f"recording-limit-{trace.turn_id}",
        )
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
            for task in (pipeline_task, receive_task, limit_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                pipeline_task,
                receive_task,
                limit_task,
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
