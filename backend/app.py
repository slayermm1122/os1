from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.realtime import create_realtime_router
from .api.routes import create_router
from .config import settings
from .core.orchestrator import TurnOrchestrator
from .core.rate_limit import SlidingWindowRateLimiter
from .core.security import is_allowed_websocket, is_local_http_request
from .core.sessions import SessionStore
from .gateways.knowledge import SQLiteFTSKnowledgeGateway
from .gateways.llm import XAILLMGateway
from .gateways.stt import ElevenLabsSTTGateway
from .gateways.tts import ElevenLabsTTSGateway
from .services import ApplicationServices
from .telemetry import SQLiteTelemetryRecorder
from .version import APP_VERSION


telemetry = SQLiteTelemetryRecorder(
    settings.telemetry_db_path,
    enabled=settings.telemetry_enabled,
    queue_size=settings.telemetry_queue_size,
    manage_parent_permissions=(
        settings.telemetry_db_path.parent.resolve()
        == (settings.root_dir / "data").resolve()
    ),
)
knowledge = SQLiteFTSKnowledgeGateway(settings)
orchestrator = TurnOrchestrator(
    settings=settings,
    llm=XAILLMGateway(settings),
    stt=ElevenLabsSTTGateway(settings),
    tts=ElevenLabsTTSGateway(settings),
    knowledge=knowledge,
    sessions=SessionStore(
        max_turns=settings.max_history_turns,
        max_sessions=settings.max_sessions,
        ttl_seconds=settings.session_ttl_seconds,
    ),
    telemetry=telemetry,
)
services = ApplicationServices(
    settings=settings,
    orchestrator=orchestrator,
    knowledge=knowledge,
    telemetry=telemetry,
    rate_limiter=SlidingWindowRateLimiter(
        requests=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
    ),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await telemetry.start()
    await _initialize_knowledge()
    try:
        yield
    finally:
        await telemetry.close()


app = FastAPI(
    title="OS1 Voice Agent",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.state.services = services
app.mount("/assets", StaticFiles(directory=settings.frontend_dir), name="assets")
app.include_router(create_router(services))
app.include_router(create_realtime_router(services))


@app.middleware("http")
async def rate_limit_api(request: Request, call_next):
    if settings.enforce_local_access and not is_local_http_request(
        request.client.host if request.client else None,
        request.headers.get("host"),
    ):
        return _secured_response(
            JSONResponse({"detail": "OS1 v0.02.01 only accepts local requests."}, status_code=403)
        )
    origin = request.headers.get("origin")
    if origin and not is_allowed_websocket(
        request.client.host if request.client else None,
        request.headers.get("host"),
        origin,
    ):
        return _secured_response(
            JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
        )
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.max_http_body_bytes:
                return _secured_response(
                    JSONResponse({"detail": "Request body is too large."}, status_code=413)
                )
        except ValueError:
            return _secured_response(JSONResponse({"detail": "Invalid Content-Length."}, status_code=400))
    if not request.url.path.startswith("/api/") or request.url.path == "/api/health":
        return _secured_response(await call_next(request))
    client_host = request.client.host if request.client else "unknown"
    if not services.rate_limiter.consume(client_host):
        return _secured_response(JSONResponse(
            {"detail": "Too many requests. Please wait before trying again."},
            status_code=429,
        ))
    return _secured_response(await call_next(request))


def _secured_response(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "media-src 'self' blob:; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
        "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    return response


async def _initialize_knowledge() -> None:
    import asyncio

    await asyncio.to_thread(knowledge.ensure_index)
    if knowledge.enabled:
        await asyncio.to_thread(knowledge.reindex)
