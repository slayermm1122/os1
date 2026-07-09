from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .knowledge import KnowledgeBase
from .llm import Message, build_messages, LLMClient
from .voice import VoiceClient


app = FastAPI(title="OS1 Voice Agent")
voice_client = VoiceClient(settings)
llm_client = LLMClient(settings)
knowledge_base = KnowledgeBase(settings)
sessions: dict[str, list[Message]] = {}
session_touched_at: dict[str, float] = {}
request_timestamps: dict[str, list[float]] = {}
logger = logging.getLogger("os1")

app.mount("/assets", StaticFiles(directory=settings.frontend_dir), name="assets")


class ChatRequest(BaseModel):
    text: str
    session_id: str | None = None


class TTSRequest(BaseModel):
    text: str


@app.middleware("http")
async def rate_limit_api(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    if request.url.path == "/api/health":
        return await call_next(request)

    now = time.monotonic()
    client_host = request.client.host if request.client else "unknown"
    window_start = now - settings.rate_limit_window_seconds
    timestamps = [ts for ts in request_timestamps.get(client_host, []) if ts >= window_start]
    if len(timestamps) >= settings.rate_limit_requests:
        return JSONResponse(
            {"detail": "Too many requests. Please wait before trying again."},
            status_code=429,
        )
    timestamps.append(now)
    request_timestamps[client_host] = timestamps
    return await call_next(request)


@app.on_event("startup")
async def startup() -> None:
    knowledge_base.ensure_index()
    if knowledge_base.enabled:
        knowledge_base.reindex()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(settings.frontend_dir / "index.html")


@app.get("/api/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.llm_model,
        "llm_reasoning_effort": settings.llm_reasoning_effort,
        "knowledge_enabled": knowledge_base.enabled,
        "elevenlabs_stt_model": settings.elevenlabs_stt_model,
        "elevenlabs_tts_model": settings.elevenlabs_tts_model,
        "elevenlabs_stream_output_format": settings.elevenlabs_stream_output_format,
        "has_elevenlabs_key": bool(settings.elevenlabs_api_key),
        "has_elevenlabs_voice_id": bool(settings.elevenlabs_voice_id),
        "has_llm_key": bool(settings.llm_api_key),
    }


@app.post("/api/stt")
async def stt(
    file: UploadFile = File(...),
    voice_api_key: str | None = Header(None, alias="X-OS1-Voice-API-Key"),
) -> dict[str, object]:
    data = await _read_limited_upload(file)
    try:
        result = await voice_client.transcribe_upload(
            filename=file.filename or "speech.webm",
            content_type=file.content_type,
            data=data,
            api_key=voice_api_key,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=_public_error(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"text": result["text"], "raw": result["raw"]}


@app.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    brain_api_key: str | None = Header(None, alias="X-OS1-Brain-API-Key"),
) -> StreamingResponse:
    _validate_text("text", request.text, settings.max_chat_chars)
    session_id = request.session_id or _new_session_id()
    return StreamingResponse(
        _stream_chat_response(
            request.text,
            session_id,
            with_audio=False,
            brain_api_key=brain_api_key,
            voice_api_key=None,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/turn/stream")
async def turn_stream(
    file: UploadFile = File(...),
    session_id: str | None = Form(None),
    brain_api_key: str | None = Header(None, alias="X-OS1-Brain-API-Key"),
    voice_api_key: str | None = Header(None, alias="X-OS1-Voice-API-Key"),
) -> StreamingResponse:
    audio_data = await _read_limited_upload(file)
    current_session_id = session_id or _new_session_id()

    async def events() -> AsyncIterator[str]:
        yield _sse("status", {"state": "transcribing", "session_id": current_session_id})
        try:
            transcription = await voice_client.transcribe_upload(
                filename=file.filename or "speech.webm",
                content_type=file.content_type,
                data=audio_data,
                api_key=voice_api_key,
            )
            user_text = transcription["text"]
            if len(user_text) > settings.max_chat_chars:
                yield _sse("error", {"message": "Transcription is too long."})
                return
            yield _sse("transcript", {"text": user_text, "session_id": current_session_id})
            if not user_text:
                yield _sse("error", {"message": "没有识别到语音。"})
                return

            async for event in _stream_chat_response(
                user_text,
                current_session_id,
                with_audio=True,
                brain_api_key=brain_api_key,
                voice_api_key=voice_api_key,
            ):
                yield event
        except Exception as exc:
            yield _sse("error", {"message": _public_error(exc)})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/tts")
async def tts(
    request: TTSRequest,
    voice_api_key: str | None = Header(None, alias="X-OS1-Voice-API-Key"),
) -> StreamingResponse:
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    _validate_text("text", text, settings.max_tts_chars)

    try:
        stream = voice_client.stream_tts(text, api_key=voice_api_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return StreamingResponse(stream, media_type=settings.tts_media_type)


@app.post("/api/knowledge/reindex")
async def knowledge_reindex() -> dict[str, object]:
    chunks = knowledge_base.reindex()
    return {
        "ok": True,
        "enabled": knowledge_base.enabled,
        "chunks": chunks,
        "docs_dir": str(settings.knowledge_docs_dir),
    }


async def _stream_chat_response(
    user_text: str,
    session_id: str,
    *,
    with_audio: bool,
    brain_api_key: str | None,
    voice_api_key: str | None,
) -> AsyncIterator[str]:
    history = _get_history(session_id)
    hits = knowledge_base.search(user_text) if knowledge_base.enabled else []
    if hits:
        yield _sse(
            "knowledge",
            {
                "hits": [
                    {"path": hit.path, "chunk": hit.chunk, "snippet": hit.snippet}
                    for hit in hits
                ]
            },
        )

    messages = build_messages(
        system_prompt=settings.system_prompt,
        user_text=user_text,
        history=history,
        knowledge_context=knowledge_base.format_hits(hits) if hits else "",
    )

    assistant_text = ""
    yield _sse("status", {"state": "thinking", "session_id": session_id})
    if with_audio:
        async for event in _stream_chat_response_with_audio(
            messages,
            user_text,
            session_id,
            brain_api_key=brain_api_key,
            voice_api_key=voice_api_key,
        ):
            yield event
        return

    try:
        async for delta in llm_client.stream_reply(messages, api_key=brain_api_key):
            assistant_text += delta
            yield _sse("delta", {"text": delta, "session_id": session_id})
    except Exception as exc:
        yield _sse("error", {"message": _public_error(exc)})
        return

    _append_history(session_id, user_text, assistant_text)
    yield _sse("done", {"text": assistant_text, "session_id": session_id})


async def _stream_chat_response_with_audio(
    messages: list[Message],
    user_text: str,
    session_id: str,
    *,
    brain_api_key: str | None,
    voice_api_key: str | None,
) -> AsyncIterator[str]:
    event_queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()
    text_queue: asyncio.Queue[str | None] = asyncio.Queue()
    assistant_text = ""
    speech_buffer = ""

    async def text_chunks() -> AsyncIterator[str]:
        while True:
            chunk = await text_queue.get()
            if chunk is None:
                break
            yield chunk

    async def produce_llm() -> None:
        nonlocal assistant_text, speech_buffer
        try:
            async for delta in llm_client.stream_reply(messages, api_key=brain_api_key):
                assistant_text += delta
                await event_queue.put(("delta", {"text": delta, "session_id": session_id}))

                speech_buffer += delta
                ready_chunks, speech_buffer = _pop_ready_speech_chunks(speech_buffer)
                for chunk in ready_chunks:
                    await text_queue.put(chunk)
                    await event_queue.put(("display", {"text": chunk, "session_id": session_id}))

            final_chunk = speech_buffer.strip()
            if final_chunk:
                await text_queue.put(final_chunk)
                await event_queue.put(("display", {"text": final_chunk, "session_id": session_id}))

            _append_history(session_id, user_text, assistant_text)
            await event_queue.put(("done", {"text": assistant_text, "session_id": session_id}))
        except Exception as exc:
            await event_queue.put(("error", {"message": _public_error(exc)}))
        finally:
            await text_queue.put(None)
            await event_queue.put(("_complete", {"task": "llm"}))

    async def produce_tts() -> None:
        try:
            async for audio_chunk in voice_client.stream_tts_websocket(
                text_chunks(),
                api_key=voice_api_key,
            ):
                await event_queue.put(
                    (
                        "audio",
                        {
                            "audio": base64.b64encode(audio_chunk).decode("ascii"),
                            "mime_type": settings.tts_stream_media_type,
                            "format": settings.elevenlabs_stream_output_format,
                            "sample_rate": settings.tts_stream_sample_rate,
                            "session_id": session_id,
                        },
                    )
                )
            await event_queue.put(("audio_done", {"session_id": session_id}))
        except Exception as exc:
            await event_queue.put(("tts_error", {"message": _public_error(exc), "session_id": session_id}))
        finally:
            await event_queue.put(("_complete", {"task": "tts"}))

    tasks = [asyncio.create_task(produce_llm()), asyncio.create_task(produce_tts())]
    completed = 0
    try:
        while completed < len(tasks):
            event, data = await event_queue.get()
            if event == "_complete":
                completed += 1
                continue
            yield _sse(event, data)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _append_history(session_id: str, user_text: str, assistant_text: str) -> None:
    _purge_sessions()
    history = sessions.setdefault(session_id, [])
    history.extend(
        [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
    )
    max_messages = max(settings.max_history_turns, 1) * 2
    if len(history) > max_messages:
        del history[:-max_messages]
    session_touched_at[session_id] = time.monotonic()
    _trim_sessions()


def _get_history(session_id: str) -> list[Message]:
    _purge_sessions()
    history = sessions.get(session_id, [])
    if history:
        session_touched_at[session_id] = time.monotonic()
    return history


def _purge_sessions() -> None:
    if settings.session_ttl_seconds <= 0:
        return
    cutoff = time.monotonic() - settings.session_ttl_seconds
    expired = [
        session_id
        for session_id, touched_at in session_touched_at.items()
        if touched_at < cutoff
    ]
    for session_id in expired:
        sessions.pop(session_id, None)
        session_touched_at.pop(session_id, None)


def _trim_sessions() -> None:
    if settings.max_sessions <= 0:
        sessions.clear()
        session_touched_at.clear()
        return
    overflow = len(sessions) - settings.max_sessions
    if overflow <= 0:
        return
    oldest = sorted(session_touched_at.items(), key=lambda item: item[1])[:overflow]
    for session_id, _ in oldest:
        sessions.pop(session_id, None)
        session_touched_at.pop(session_id, None)


def _new_session_id() -> str:
    return uuid.uuid4().hex


async def _read_limited_upload(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Upload is too large. Limit is {settings.max_upload_bytes} bytes.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_text(field: str, value: str, max_chars: int) -> None:
    if max_chars > 0 and len(value) > max_chars:
        raise HTTPException(
            status_code=413,
            detail=f"{field} is too long. Limit is {max_chars} characters.",
        )


def _pop_ready_speech_chunks(buffer: str) -> tuple[list[str], str]:
    chunks: list[str] = []
    sentence_endings = ".!?。！？\n"
    soft_breaks = ",;:，；、"
    min_chars = 12
    max_chars = 120

    while True:
        split_at = -1
        for index, char in enumerate(buffer):
            if index + 1 >= min_chars and char in sentence_endings:
                split_at = index + 1
                break

        if split_at == -1 and len(buffer) >= max_chars:
            candidates = [buffer.rfind(char, 0, max_chars) for char in soft_breaks + " "]
            split_at = max(candidates)
            if split_at < min_chars:
                split_at = max_chars
            else:
                split_at += 1

        if split_at == -1:
            break

        chunk = buffer[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        buffer = buffer[split_at:].lstrip()

    return chunks, buffer


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _public_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        logger.warning(
            "Upstream HTTP error: status=%s url=%s",
            exc.response.status_code,
            exc.request.url,
        )
        return "Upstream service rejected the request."
    if isinstance(exc, httpx.HTTPError):
        logger.warning("Upstream transport error: %s", exc.__class__.__name__)
        return "Upstream service is unavailable."
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        logger.warning("Upstream timeout: %s", exc.__class__.__name__)
        return "Upstream service timed out."
    if isinstance(exc, RuntimeError):
        message = str(exc)
        if "API_KEY" in message or "configured" in message or "required" in message:
            return message
    logger.exception("Unhandled request error")
    return "Request failed. Check server logs."
