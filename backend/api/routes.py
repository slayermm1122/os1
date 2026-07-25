from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..core.errors import GatewayError, error_info
from ..core.orchestrator import ObservedError
from ..gateways.tts import TTSAdapter
from ..services import ApplicationServices
from ..version import APP_VERSION
from .protocol import sse


class ChatRequest(BaseModel):
    text: str = Field(max_length=100_000)
    session_id: str | None = Field(default=None, max_length=128)


class TTSRequest(BaseModel):
    text: str = Field(max_length=100_000)


def create_router(services: ApplicationServices) -> APIRouter:
    router = APIRouter()
    settings = services.settings
    orchestrator = services.orchestrator

    @router.get("/")
    async def index() -> FileResponse:
        return FileResponse(settings.frontend_dir / "index.html")

    @router.get("/knowledge")
    @router.get("/knowledge/")
    async def knowledge_page() -> FileResponse:
        return FileResponse(settings.frontend_dir / "knowledge.html")

    @router.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "ok": True,
            "version": APP_VERSION,
            "telemetry_enabled": services.telemetry.enabled,
            "stt_commit_strategy": settings.elevenlabs_stt_commit_strategy,
            "has_elevenlabs_key": bool(settings.elevenlabs_api_key),
            "has_tts_key": orchestrator.tts.resolve().api_key_configured,
            "tts_providers": list(orchestrator.tts.providers),
            "tts_provider_keys": {
                provider: orchestrator.tts.resolve(provider).api_key_configured
                for provider in orchestrator.tts.providers
            },
            "default_tts_provider": orchestrator.tts.default_provider,
            "has_llm_key": bool(settings.llm_api_key),
            "brain_model": settings.llm_model,
            "llm_search_model": settings.knowledge_selector_model,
            "llm_search_reasoning_effort": settings.knowledge_selector_reasoning_effort,
            "knowledge_search_timeout_seconds": settings.knowledge_search_timeout_seconds,
            "knowledge_enabled": settings.knowledge_enabled,
            "knowledge_ui_enabled": settings.knowledge_ui_enabled,
            "default_male_voice_id": settings.elevenlabs_male_voice_id,
            "default_female_voice_id": settings.elevenlabs_female_voice_id,
        }

    @router.post("/api/connectivity/check")
    async def connectivity_check(
        brain_api_key: str | None = Header(None, alias="X-OS1-Brain-API-Key"),
        voice_api_key: str | None = Header(None, alias="X-OS1-Voice-API-Key"),
        voice_id: str | None = Header(None, alias="X-OS1-Voice-ID"),
        voice_gender: str | None = Header(None, alias="X-OS1-Voice-Gender"),
        voice_provider: str | None = Header(None, alias="X-OS1-Voice-Provider"),
        stt_api_key: str | None = Header(None, alias="X-OS1-STT-API-Key"),
        tts_api_key: str | None = Header(None, alias="X-OS1-TTS-API-Key"),
    ) -> dict[str, object]:
        if services.connectivity is None:
            raise HTTPException(status_code=503, detail="Provider checks are unavailable.")
        try:
            selected_voice = resolve_voice_id(
                orchestrator.tts, voice_provider, voice_id, voice_gender
            )
            return await services.connectivity.check(
                brain_api_key=brain_api_key,
                stt_api_key=stt_api_key if stt_api_key is not None else voice_api_key,
                tts_api_key=tts_api_key if tts_api_key is not None else voice_api_key,
                tts_voice_id=selected_voice,
                tts_provider=voice_provider,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/api/stt")
    async def stt(
        file: UploadFile = File(...),
        voice_api_key: str | None = Header(None, alias="X-OS1-Voice-API-Key"),
        stt_api_key: str | None = Header(None, alias="X-OS1-STT-API-Key"),
    ) -> dict[str, object]:
        data = await _read_limited_upload(file, settings.max_upload_bytes)
        trace = orchestrator.new_trace(session_id=_new_session_id(), kind="stt_only")
        try:
            result = await orchestrator.transcribe_upload_only(
                trace,
                filename=file.filename or "speech.webm",
                content_type=file.content_type,
                data=data,
                api_key=stt_api_key if stt_api_key is not None else voice_api_key,
            )
        except Exception as exc:
            raise _http_error(exc, turn_id=trace.turn_id) from exc
        return {"text": result.text, "raw": result.raw, "turn_id": trace.turn_id}

    @router.post("/api/chat/stream")
    async def chat_stream(
        request: ChatRequest,
        brain_api_key: str | None = Header(None, alias="X-OS1-Brain-API-Key"),
    ) -> StreamingResponse:
        _validate_text("text", request.text, settings.max_chat_chars)
        session_id = request.session_id or _new_session_id()
        trace = orchestrator.new_trace(session_id=session_id, kind="chat_only")

        async def events() -> AsyncIterator[str]:
            async for event in orchestrator.stream_chat(
                trace,
                user_text=request.text,
                with_audio=False,
                brain_api_key=brain_api_key,
                voice_api_key=None,
                voice_id=None,
            ):
                yield sse(event)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-OS1-Turn-ID": trace.turn_id,
            },
        )

    @router.post("/api/turn/stream")
    async def turn_stream(
        file: UploadFile = File(...),
        session_id: str | None = Form(None, max_length=128),
        brain_api_key: str | None = Header(None, alias="X-OS1-Brain-API-Key"),
        voice_api_key: str | None = Header(None, alias="X-OS1-Voice-API-Key"),
        voice_id: str | None = Header(None, alias="X-OS1-Voice-ID"),
        voice_gender: str | None = Header(None, alias="X-OS1-Voice-Gender"),
        voice_provider: str | None = Header(None, alias="X-OS1-Voice-Provider"),
        stt_api_key: str | None = Header(None, alias="X-OS1-STT-API-Key"),
        tts_api_key: str | None = Header(None, alias="X-OS1-TTS-API-Key"),
    ) -> StreamingResponse:
        data = await _read_limited_upload(file, settings.max_upload_bytes)
        current_session = session_id or _new_session_id()
        trace = orchestrator.new_trace(session_id=current_session, kind="voice_upload")
        try:
            selected_voice = resolve_voice_id(
                orchestrator.tts, voice_provider, voice_id, voice_gender
            )
        except Exception as exc:
            raise _http_error(exc, turn_id=trace.turn_id) from exc

        async def events() -> AsyncIterator[str]:
            async for event in orchestrator.stream_upload_turn(
                trace,
                filename=file.filename or "speech.webm",
                content_type=file.content_type,
                data=data,
                brain_api_key=brain_api_key,
                voice_api_key=voice_api_key,
                voice_id=selected_voice,
                tts_provider=voice_provider,
                stt_api_key=stt_api_key,
                tts_api_key=tts_api_key,
            ):
                yield sse(event)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-OS1-Turn-ID": trace.turn_id,
            },
        )

    @router.post("/api/tts")
    async def tts(
        request: TTSRequest,
        voice_api_key: str | None = Header(None, alias="X-OS1-Voice-API-Key"),
        voice_id: str | None = Header(None, alias="X-OS1-Voice-ID"),
        voice_gender: str | None = Header(None, alias="X-OS1-Voice-Gender"),
        voice_provider: str | None = Header(None, alias="X-OS1-Voice-Provider"),
        tts_api_key: str | None = Header(None, alias="X-OS1-TTS-API-Key"),
    ) -> StreamingResponse:
        text = request.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        _validate_text("text", text, settings.max_tts_chars)
        trace = orchestrator.new_trace(session_id=_new_session_id(), kind="tts_only")
        try:
            tts_gateway = orchestrator.tts.resolve(voice_provider)
            selected_voice = tts_gateway.resolve_voice_id(voice_id, voice_gender)
        except Exception as exc:
            raise _http_error(exc, turn_id=trace.turn_id) from exc
        stream = orchestrator.stream_tts_http(
            trace,
            text=text,
            api_key=tts_api_key if tts_api_key is not None else voice_api_key,
            voice_id=selected_voice,
            tts_provider=voice_provider,
        )
        try:
            first_chunk = await anext(stream)
        except StopAsyncIteration as exc:
            error = GatewayError(
                stage="tts",
                provider=tts_gateway.provider,
                code="empty_audio",
                public_message="Voice service returned no audio.",
            )
            info = error_info(error)
            services.telemetry.record_error(trace, info)
            trace.finish("failed")
            raise _http_error(ObservedError(info), turn_id=trace.turn_id) from exc
        except Exception as exc:
            raise _http_error(exc, turn_id=trace.turn_id) from exc

        async def audio_body() -> AsyncIterator[bytes]:
            yield first_chunk
            async for chunk in stream:
                yield chunk

        return StreamingResponse(
            audio_body(),
            media_type=tts_gateway.http_media_type,
            headers={"X-OS1-Turn-ID": trace.turn_id},
        )

    @router.get("/api/knowledge/overview")
    async def knowledge_overview() -> dict[str, object]:
        if services.knowledge_browser is None:
            raise HTTPException(status_code=503, detail="Knowledge browsing is unavailable.")
        return await _in_thread(services.knowledge_browser.overview)

    @router.get("/api/knowledge/wiki/{wiki_id}")
    async def knowledge_wiki_page(wiki_id: str) -> dict[str, object]:
        if services.knowledge_browser is None:
            raise HTTPException(status_code=503, detail="Knowledge browsing is unavailable.")
        page = await _in_thread(lambda: services.knowledge_browser.wiki_page(wiki_id))
        if page is None:
            raise HTTPException(status_code=404, detail="Wiki page was not found.")
        return page

    @router.get("/api/knowledge/chunks/{chunk_id}")
    async def knowledge_chunk(chunk_id: str) -> dict[str, object]:
        if services.knowledge_browser is None:
            raise HTTPException(status_code=503, detail="Knowledge browsing is unavailable.")
        chunk = await _in_thread(lambda: services.knowledge_browser.chunk(chunk_id))
        if chunk is None:
            raise HTTPException(status_code=404, detail="Chunk was not found.")
        return chunk

    return router


def resolve_voice_id(
    tts: TTSAdapter,
    provider: str | None,
    voice_id: str | None,
    voice_gender: str | None,
) -> str | None:
    return tts.resolve_voice_id(provider, voice_id, voice_gender)


async def _read_limited_upload(file: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=f"Upload is too large. Limit is {limit} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_text(field: str, value: str, max_chars: int) -> None:
    if max_chars > 0 and len(value) > max_chars:
        raise HTTPException(
            status_code=413,
            detail=f"{field} is too long. Limit is {max_chars} characters.",
        )


def _http_error(exc: Exception, *, turn_id: str | None = None) -> HTTPException:
    info = exc.info if isinstance(exc, ObservedError) else error_info(exc)
    if info.code in {
        "api_key_missing",
        "voice_missing",
        "configuration_error",
        "unsupported_provider",
    }:
        status = 400
    elif info.upstream_status in {401, 402, 403, 429}:
        status = info.upstream_status
    else:
        status = 502
    detail: dict[str, object] = {
        "message": info.public_message,
        "error_id": info.error_id,
        "stage": info.stage,
        "provider": info.provider,
        "code": info.code,
        "retryable": info.retryable,
        "upstream_status": info.upstream_status,
        "request_id": info.request_id,
        "provider_detail": info.provider_detail,
    }
    if turn_id:
        detail["turn_id"] = turn_id
    return HTTPException(
        status_code=status,
        detail=detail,
    )


async def _in_thread(function):
    import asyncio

    return await asyncio.to_thread(function)


def _new_session_id() -> str:
    return uuid.uuid4().hex
