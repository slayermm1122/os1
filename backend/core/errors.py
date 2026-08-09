from __future__ import annotations

import re
import traceback
import uuid
from dataclasses import dataclass, field

import httpx


_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)[\"']?(authorization|proxy-authorization)[\"']?\s*[:=]\s*"
        r"[\"']?(?:bearer|basic|token)?\s*[A-Za-z0-9._~+/=-]{8,}[\"']?"
    ),
    re.compile(r"xai-[A-Za-z0-9_-]{12,}"),
    re.compile(r"sk_[A-Za-z0-9_-]{12,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"AQ\.[A-Za-z0-9_-]{12,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
    re.compile(
        r"(?i)[\"']?(xi-api-key|x-goog-api-key|api[_-]?key)[\"']?\s*[:=]\s*"
        r"[\"']?[^\s,;\"'}]+[\"']?"
    ),
)


def redact(value: str) -> str:
    sanitized = value
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("<redacted>", sanitized)
    return sanitized[:8000]


@dataclass
class GatewayError(Exception):
    stage: str
    provider: str
    code: str
    public_message: str
    technical_message: str = ""
    retryable: bool = False
    upstream_status: int | None = None
    request_id: str | None = None
    provider_detail: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.public_message)


@dataclass(frozen=True)
class ErrorInfo:
    error_id: str
    stage: str
    provider: str | None
    code: str
    public_message: str
    technical_message: str
    retryable: bool
    upstream_status: int | None
    request_id: str | None
    provider_detail: dict[str, str]
    exception_type: str
    stack_trace: str

    def payload(self, turn_id: str) -> dict[str, object]:
        return {
            "message": self.public_message,
            "turn_id": turn_id,
            "error_id": self.error_id,
            "stage": self.stage,
            "provider": self.provider,
            "code": self.code,
            "retryable": self.retryable,
            "upstream_status": self.upstream_status,
            "request_id": self.request_id,
            "provider_detail": self.provider_detail,
        }


def error_info(exc: Exception, *, default_stage: str = "pipeline") -> ErrorInfo:
    provider_detail: dict[str, str] = {}
    if isinstance(exc, GatewayError):
        stage = exc.stage
        provider = exc.provider
        code = exc.code
        public_message = exc.public_message
        technical_message = exc.technical_message or str(exc)
        retryable = exc.retryable
        upstream_status = exc.upstream_status
        request_id = exc.request_id
        provider_detail = exc.provider_detail
    elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        stage = default_stage
        provider = None
        code = "upstream_timeout"
        public_message = "Upstream service timed out."
        technical_message = str(exc)
        retryable = True
        upstream_status = None
        request_id = None
    elif isinstance(exc, httpx.HTTPStatusError):
        stage = default_stage
        provider = None
        code = "upstream_rejected"
        public_message = "Upstream service rejected the request."
        technical_message = str(exc)
        retryable = exc.response.status_code >= 500 or exc.response.status_code == 429
        upstream_status = exc.response.status_code
        request_id = exc.response.headers.get("request-id") or exc.response.headers.get("x-request-id")
        provider_detail = parse_provider_error(exc.response)
    elif isinstance(exc, httpx.HTTPError):
        stage = default_stage
        provider = None
        code = "upstream_unavailable"
        public_message = "Upstream service is unavailable."
        technical_message = str(exc)
        retryable = True
        upstream_status = None
        request_id = None
    elif isinstance(exc, RuntimeError) and any(
        marker in str(exc) for marker in ("API_KEY", "configured", "required")
    ):
        stage = default_stage
        provider = None
        code = "configuration_error"
        public_message = str(exc)
        technical_message = str(exc)
        retryable = False
        upstream_status = None
        request_id = None
    else:
        stage = default_stage
        provider = None
        code = "internal_error"
        public_message = "Request failed. Check local diagnostics."
        technical_message = str(exc)
        retryable = False
        upstream_status = None
        request_id = None

    return ErrorInfo(
        error_id=uuid.uuid4().hex,
        stage=stage,
        provider=provider,
        code=code,
        public_message=redact(public_message),
        technical_message=redact(technical_message),
        retryable=retryable,
        upstream_status=upstream_status,
        request_id=redact(request_id) if request_id else None,
        provider_detail={key: redact(value) for key, value in provider_detail.items()},
        exception_type=exc.__class__.__name__,
        stack_trace=redact("".join(traceback.format_exception(exc))),
    )


def parse_provider_error(response: httpx.Response) -> dict[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    detail = payload.get("detail", payload.get("error", payload))
    if isinstance(detail, str):
        return {"message": redact(detail)}
    if not isinstance(detail, dict):
        return {}
    result: dict[str, str] = {}
    for key in ("type", "code", "status", "message", "request_id"):
        value = detail.get(key)
        if value is not None:
            result[key] = redact(str(value))
    return result
