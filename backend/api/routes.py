from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
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


class VoiceSelectionRequest(BaseModel):
    voice_id: str = Field(min_length=5, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class VoiceProfileRequest(BaseModel):
    vad_silence_threshold_secs: float = Field(ge=0.3, le=3.0)
    vad_threshold: float = Field(ge=0.1, le=0.9)


class PersonaRequest(BaseModel):
    assistant_name: str = Field(default="", max_length=40)
    user_name: str = Field(default="", max_length=40)
    persona: str = Field(default="default", pattern=r"^(default|concise|conversational)$")


def create_router(services: ApplicationServices) -> APIRouter:
    router = APIRouter()
    settings = services.settings
    orchestrator = services.orchestrator

    @router.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            settings.frontend_dir / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Clear-Site-Data": '"storage"',
            },
        )

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
            "selected_voice_id": settings.elevenlabs_voice_id,
            "selected_voice_language": settings.elevenlabs_voice_language,
            "assistant_name": settings.assistant_name,
            "user_name": settings.user_name,
            "assistant_persona": settings.assistant_persona,
            "vad_silence_threshold_secs": settings.elevenlabs_stt_vad_silence_threshold_secs,
            "vad_threshold": settings.elevenlabs_stt_vad_threshold,
            "has_llm_key": bool(settings.llm_api_key),
            "brain_model": settings.llm_model,
            "default_male_voice_id": settings.elevenlabs_male_voice_id,
            "default_female_voice_id": settings.elevenlabs_female_voice_id,
            "default_voices": [
                {
                    "voice_id": settings.elevenlabs_male_voice_id,
                    "name": "George",
                    "gender": "male",
                    "is_default": True,
                    "primary_language": "en",
                    "primary_locale": "en-US",
                    "preview_available": True,
                },
                {
                    "voice_id": settings.elevenlabs_female_voice_id,
                    "name": "Sarah",
                    "gender": "female",
                    "is_default": True,
                    "primary_language": "en",
                    "primary_locale": "en-US",
                    "preview_available": True,
                },
            ],
        }

    @router.post("/api/connectivity/check")
    async def connectivity_check() -> dict[str, object]:
        if services.connectivity is None:
            raise HTTPException(status_code=503, detail="Provider checks are unavailable.")
        try:
            selected_voice = resolve_voice_id(orchestrator.tts, None, None, None)
            return await services.connectivity.check(
                brain_api_key=None,
                stt_api_key=None,
                tts_api_key=None,
                tts_voice_id=selected_voice,
                tts_provider=None,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/api/tts/voices")
    async def tts_voices() -> dict[str, object]:
        if services.voice_catalog is None:
            raise HTTPException(status_code=503, detail="Voice library is unavailable.")
        try:
            voices = await services.voice_catalog.list_voices()
            return {"provider": services.voice_catalog.provider, "voices": voices}
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/api/tts/voices/{voice_id}/preview")
    async def tts_voice_preview(voice_id: str) -> Response:
        if services.voice_catalog is None:
            raise HTTPException(status_code=503, detail="Voice library is unavailable.")
        try:
            audio, media_type = await services.voice_catalog.preview(voice_id)
            return Response(audio, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/api/settings/voice")
    async def select_voice(request: VoiceSelectionRequest) -> dict[str, object]:
        if services.voice_catalog is None or services.local_settings is None:
            raise HTTPException(status_code=503, detail="Local voice settings are unavailable.")
        try:
            voices = await services.voice_catalog.list_voices()
            selectable_ids = {
                settings.elevenlabs_male_voice_id,
                settings.elevenlabs_female_voice_id,
                *(str(voice.get("voice_id") or "") for voice in voices),
            }
            if request.voice_id not in selectable_ids:
                raise HTTPException(status_code=400, detail="Voice is not available to this account.")
            selected_voice = next(
                (voice for voice in voices if str(voice.get("voice_id") or "") == request.voice_id),
                None,
            )
            language = "en"
            if selected_voice is not None:
                primary_language = str(selected_voice.get("primary_language") or "").lower()
                primary_locale = str(selected_voice.get("primary_locale") or "").lower()
                if primary_language.startswith("zh") or primary_locale.startswith(("zh", "cmn")):
                    language = "zh"
            await _in_thread(
                lambda: services.local_settings.select_voice(request.voice_id, language)
            )
            return {
                "voice_id": settings.elevenlabs_voice_id,
                "language": settings.elevenlabs_voice_language,
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/api/settings/voice-profile")
    async def update_voice_profile(request: VoiceProfileRequest) -> dict[str, object]:
        if services.local_settings is None:
            raise HTTPException(status_code=503, detail="Local voice settings are unavailable.")
        try:
            await _in_thread(
                lambda: services.local_settings.update_voice_profile(
                    vad_silence_threshold_secs=request.vad_silence_threshold_secs,
                    vad_threshold=request.vad_threshold,
                )
            )
            return {
                "vad_silence_threshold_secs": settings.elevenlabs_stt_vad_silence_threshold_secs,
                "vad_threshold": settings.elevenlabs_stt_vad_threshold,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/api/settings/persona")
    async def update_persona(request: PersonaRequest) -> dict[str, object]:
        if services.local_settings is None:
            raise HTTPException(status_code=503, detail="Local persona settings are unavailable.")
        try:
            await _in_thread(
                lambda: services.local_settings.update_persona(
                    assistant_name=request.assistant_name,
                    user_name=request.user_name,
                    persona=request.persona,
                )
            )
            return {
                "assistant_name": settings.assistant_name,
                "user_name": settings.user_name,
                "persona": settings.assistant_persona,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.get("/api/account")
    async def account_summary() -> dict[str, object]:
        if services.account is None:
            raise HTTPException(status_code=503, detail="Account information is unavailable.")
        try:
            return await services.account.summary()
        except Exception as exc:
            raise _http_error(exc) from exc

    @router.post("/api/stt")
    async def stt(file: UploadFile = File(...)) -> dict[str, object]:
        data = await _read_limited_upload(file, settings.max_upload_bytes)
        trace = orchestrator.new_trace(session_id=_new_session_id(), kind="stt_only")
        try:
            result = await orchestrator.transcribe_upload_only(
                trace,
                filename=file.filename or "speech.webm",
                content_type=file.content_type,
                data=data,
                api_key=None,
            )
        except Exception as exc:
            raise _http_error(exc, turn_id=trace.turn_id) from exc
        return {"text": result.text, "raw": result.raw, "turn_id": trace.turn_id}

    @router.post("/api/chat/stream")
    async def chat_stream(
        request: ChatRequest,
    ) -> StreamingResponse:
        _validate_text("text", request.text, settings.max_chat_chars)
        session_id = request.session_id or _new_session_id()
        trace = orchestrator.new_trace(session_id=session_id, kind="chat_only")

        async def events() -> AsyncIterator[str]:
            async for event in orchestrator.stream_chat(
                trace,
                user_text=request.text,
                with_audio=False,
                brain_api_key=None,
                voice_api_key=None,
                voice_id=None,
                assistant_name=settings.assistant_name,
                user_name=settings.user_name,
                assistant_persona=settings.assistant_persona,
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
    ) -> StreamingResponse:
        data = await _read_limited_upload(file, settings.max_upload_bytes)
        current_session = session_id or _new_session_id()
        trace = orchestrator.new_trace(session_id=current_session, kind="voice_upload")
        try:
            selected_voice = resolve_voice_id(orchestrator.tts, None, None, None)
        except Exception as exc:
            raise _http_error(exc, turn_id=trace.turn_id) from exc

        async def events() -> AsyncIterator[str]:
            async for event in orchestrator.stream_upload_turn(
                trace,
                filename=file.filename or "speech.webm",
                content_type=file.content_type,
                data=data,
                brain_api_key=None,
                voice_api_key=None,
                voice_id=selected_voice,
                tts_provider=None,
                stt_api_key=None,
                tts_api_key=None,
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
    ) -> StreamingResponse:
        text = request.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        _validate_text("text", text, settings.max_tts_chars)
        trace = orchestrator.new_trace(session_id=_new_session_id(), kind="tts_only")
        try:
            tts_gateway = orchestrator.tts.resolve()
            selected_voice = tts_gateway.resolve_voice_id(None, None)
        except Exception as exc:
            raise _http_error(exc, turn_id=trace.turn_id) from exc
        stream = orchestrator.stream_tts_http(
            trace,
            text=text,
            api_key=None,
            voice_id=selected_voice,
            tts_provider=None,
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
