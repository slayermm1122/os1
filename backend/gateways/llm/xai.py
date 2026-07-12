from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ...config import Settings
from ...core.errors import GatewayError, parse_provider_error
from .base import AIObjectResult, LLMRequest, LLMStreamEvent, LLMUsage


class XAILLMGateway:
    provider = "xai"

    def __init__(
        self,
        settings: Settings,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.settings = settings
        self.model = model or settings.llm_model
        self.reasoning_effort = reasoning_effort or settings.llm_reasoning_effort

    def _require_api_key(self, api_key: str | None) -> str:
        resolved = (api_key or self.settings.llm_api_key).strip()
        if not resolved:
            raise GatewayError(
                stage="llm",
                provider=self.provider,
                code="api_key_missing",
                public_message="LLM API key is required.",
                technical_message="LLM_API_KEY, DEEPSEEK_API_KEY, or XAI_API_KEY is not configured.",
            )
        return resolved

    def request_snapshot(self, request: LLMRequest) -> dict[str, object]:
        return {
            "model": self.model,
            "messages": request.messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

    async def stream_text(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        api_key = self._require_api_key(request.api_key)
        payload = self.request_snapshot(request)
        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=self.settings.upstream_read_timeout_seconds,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        usage: LLMUsage | None = None
        request_id: str | None = None
        fingerprint: str | None = None
        service_tier: str | None = None
        finish_reason: str | None = None

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    self.settings.llm_chat_url,
                    headers=self._headers(api_key, request.cache_key),
                    json=payload,
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

                            request_id = str(event.get("id") or request_id or header_request_id or "") or None
                            fingerprint = str(event.get("system_fingerprint") or fingerprint or "") or None
                            service_tier = str(event.get("service_tier") or service_tier or "") or None
                            if event.get("usage"):
                                usage = _parse_usage(event["usage"])
                            for choice in event.get("choices", []):
                                if choice.get("finish_reason"):
                                    finish_reason = str(choice["finish_reason"])
                                delta = choice.get("delta") or {}
                                content = delta.get("content")
                                if content:
                                    yield LLMStreamEvent(kind="delta", text=str(content))
        except GatewayError:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            provider_detail = parse_provider_error(exc.response)
            raise GatewayError(
                stage="llm",
                provider=self.provider,
                code=_http_code(status),
                public_message="The language model rejected the request.",
                technical_message=(
                    f"{exc}; provider: {provider_detail.get('message')}"
                    if provider_detail.get("message")
                    else str(exc)
                ),
                retryable=status >= 500 or status == 429,
                upstream_status=status,
                request_id=exc.response.headers.get("x-request-id") or exc.response.headers.get("request-id"),
                provider_detail=provider_detail,
            ) from exc
        except (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError) as exc:
            raise GatewayError(
                stage="llm",
                provider=self.provider,
                code="timeout",
                public_message="The language model timed out.",
                technical_message=str(exc),
                retryable=True,
                request_id=request_id,
            ) from exc
        except httpx.HTTPError as exc:
            raise GatewayError(
                stage="llm",
                provider=self.provider,
                code="transport_error",
                public_message="The language model is unavailable.",
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
        )

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self.stream_text(request)

    async def generate_object(
        self,
        request: LLMRequest,
        *,
        schema_name: str,
        schema: dict[str, Any],
    ) -> AIObjectResult:
        api_key = self._require_api_key(request.api_key)
        payload = {
            "model": self.model,
            "messages": request.messages,
            "temperature": 0,
            "max_tokens": 128,
            "reasoning_effort": self.reasoning_effort,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
            "stream": False,
        }
        request_id: str | None = None
        try:
            async with httpx.AsyncClient(timeout=self._timeout()) as client:
                response = await client.post(
                    self.settings.llm_chat_url,
                    headers=self._headers(api_key, request.cache_key),
                    json=payload,
                )
                response.raise_for_status()
                request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                body = response.json()
                raw_text = str(body["choices"][0]["message"]["content"] or "")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise GatewayError(
                stage="knowledge", provider=self.provider, code=_http_code(status),
                public_message="The knowledge selector rejected the request.", technical_message=str(exc),
                retryable=status >= 500 or status == 429, upstream_status=status,
                request_id=exc.response.headers.get("x-request-id") or exc.response.headers.get("request-id"),
                provider_detail=parse_provider_error(exc.response),
            ) from exc
        except (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError) as exc:
            raise GatewayError(
                stage="knowledge", provider=self.provider, code="timeout",
                public_message="The knowledge selector timed out.", technical_message=str(exc),
                retryable=True, request_id=request_id,
            ) from exc
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise GatewayError(
                stage="knowledge", provider=self.provider, code="invalid_response",
                public_message="The knowledge selector returned an invalid response.",
                technical_message=str(exc), retryable=True, request_id=request_id,
            ) from exc

        try:
            value = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise GatewayError(
                stage="knowledge", provider=self.provider, code="invalid_object",
                public_message="The knowledge selector returned invalid JSON.",
                technical_message=str(exc), request_id=str(body.get("id") or request_id or "") or None,
            ) from exc
        if not isinstance(value, dict):
            raise GatewayError(
                stage="knowledge", provider=self.provider, code="invalid_object",
                public_message="The knowledge selector returned an invalid object.",
            )
        return AIObjectResult(
            value=value,
            raw_text=raw_text,
            usage=_parse_usage(body["usage"]) if body.get("usage") else None,
            finish_reason=str(body["choices"][0].get("finish_reason") or "") or None,
            request_id=str(body.get("id") or request_id or "") or None,
            system_fingerprint=str(body.get("system_fingerprint") or "") or None,
            service_tier=str(body.get("service_tier") or "") or None,
        )

    def _headers(self, api_key: str, cache_key: str | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if cache_key:
            headers["x-grok-conv-id"] = cache_key[:256]
        return headers

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=self.settings.upstream_read_timeout_seconds,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )


def _parse_usage(payload: dict[str, object]) -> LLMUsage:
    prompt_details = payload.get("prompt_tokens_details") or {}
    completion_details = payload.get("completion_tokens_details") or {}
    return LLMUsage(
        prompt_tokens=_integer(payload.get("prompt_tokens")),
        completion_tokens=_integer(payload.get("completion_tokens")),
        total_tokens=_integer(payload.get("total_tokens")),
        cached_tokens=_integer(prompt_details.get("cached_tokens")) if isinstance(prompt_details, dict) else None,
        reasoning_tokens=(
            _integer(completion_details.get("reasoning_tokens"))
            if isinstance(completion_details, dict)
            else None
        ),
        cost_usd_ticks=_integer(payload.get("cost_in_usd_ticks")),
    )


def _integer(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _http_code(status: int) -> str:
    if status in {401, 403}:
        return "authentication_failed"
    if status == 402:
        return "payment_required"
    if status == 429:
        return "rate_limited"
    return "upstream_rejected"
