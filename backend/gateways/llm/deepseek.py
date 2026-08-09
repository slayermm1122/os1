from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator

import httpx

from ...config import Settings
from ...core.errors import GatewayError, parse_provider_error
from .base import LLMRequest, LLMStreamEvent, LLMUsage


class DeepSeekLLMGateway:
    provider = "deepseek"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.deepseek_model
        self.reasoning_setting = settings.deepseek_thinking

    @property
    def api_key_configured(self) -> bool:
        return bool(self.settings.deepseek_api_key.strip())

    def request_snapshot(self, request: LLMRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": request.messages,
            "thinking": {"type": self.reasoning_setting},
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.cache_key:
            payload["user_id"] = _user_id(request.cache_key)
        return payload

    async def stream_text(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        api_key = self.settings.deepseek_api_key.strip()
        if not api_key:
            raise GatewayError(
                stage="llm",
                provider=self.provider,
                code="api_key_missing",
                public_message="DeepSeek API key is required.",
                technical_message="DEEPSEEK_API_KEY is not configured.",
            )

        usage: LLMUsage | None = None
        request_id: str | None = None
        response_model: str | None = None
        fingerprint: str | None = None
        service_tier: str | None = None
        finish_reason: str | None = None
        timeout = _timeout(self.settings)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    self.settings.deepseek_chat_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=self.request_snapshot(request),
                ) as response:
                    response.raise_for_status()
                    header_request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                    request_id = header_request_id
                    async with asyncio.timeout(self.settings.upstream_stream_timeout_seconds):
                        async for line in response.aiter_lines():
                            if not line or line.startswith(":") or not line.startswith("data:"):
                                continue
                            data = line.removeprefix("data:").strip()
                            if data == "[DONE]":
                                break
                            try:
                                event = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(event, dict):
                                continue
                            request_id = str(event.get("id") or request_id or "") or None
                            response_model = str(event.get("model") or response_model or "") or None
                            fingerprint = str(event.get("system_fingerprint") or fingerprint or "") or None
                            service_tier = str(event.get("service_tier") or service_tier or "") or None
                            if isinstance(event.get("usage"), dict):
                                usage = _parse_usage(event["usage"])
                            for choice in event.get("choices") or []:
                                if not isinstance(choice, dict):
                                    continue
                                if choice.get("finish_reason"):
                                    finish_reason = str(choice["finish_reason"])
                                delta = choice.get("delta") or {}
                                if not isinstance(delta, dict):
                                    continue
                                content = delta.get("content")
                                if content:
                                    yield LLMStreamEvent(kind="delta", text=str(content))
        except httpx.HTTPStatusError as exc:
            raise _status_error(exc, request_id) from exc
        except (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError) as exc:
            raise GatewayError(
                stage="llm",
                provider=self.provider,
                code="timeout",
                public_message="DeepSeek timed out.",
                technical_message=str(exc),
                retryable=True,
                request_id=request_id,
            ) from exc
        except httpx.HTTPError as exc:
            raise GatewayError(
                stage="llm",
                provider=self.provider,
                code="transport_error",
                public_message="DeepSeek is unavailable.",
                technical_message=str(exc),
                retryable=True,
                request_id=request_id,
            ) from exc

        yield LLMStreamEvent(
            kind="complete",
            usage=usage,
            finish_reason=finish_reason,
            request_id=request_id,
            system_fingerprint=fingerprint,
            service_tier=service_tier,
            response_model=response_model,
        )


def _parse_usage(payload: dict[str, object]) -> LLMUsage:
    completion_details = payload.get("completion_tokens_details") or {}
    return LLMUsage(
        prompt_tokens=_integer(payload.get("prompt_tokens")),
        completion_tokens=_integer(payload.get("completion_tokens")),
        total_tokens=_integer(payload.get("total_tokens")),
        cached_tokens=_integer(payload.get("prompt_cache_hit_tokens")),
        reasoning_tokens=(
            _integer(completion_details.get("reasoning_tokens"))
            if isinstance(completion_details, dict)
            else None
        ),
    )


def _user_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value)[:512]
    return cleaned or "os1"


def _integer(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _timeout(settings: Settings) -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.upstream_connect_timeout_seconds,
        read=settings.upstream_read_timeout_seconds,
        write=settings.upstream_write_timeout_seconds,
        pool=settings.upstream_pool_timeout_seconds,
    )


def _status_error(exc: httpx.HTTPStatusError, request_id: str | None) -> GatewayError:
    status = exc.response.status_code
    detail = parse_provider_error(exc.response)
    code = "authentication_failed" if status in {401, 403} else "rate_limited" if status == 429 else "upstream_rejected"
    return GatewayError(
        stage="llm",
        provider="deepseek",
        code=code,
        public_message="DeepSeek rejected the request.",
        technical_message=detail.get("message") or str(exc),
        retryable=status >= 500 or status == 429,
        upstream_status=status,
        request_id=exc.response.headers.get("x-request-id") or request_id,
        provider_detail=detail,
    )
