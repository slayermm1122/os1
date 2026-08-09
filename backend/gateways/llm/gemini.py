from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from ...config import Settings
from ...core.errors import GatewayError, parse_provider_error
from ...core.messages import Message
from .base import LLMRequest, LLMStreamEvent, LLMUsage


class GeminiLLMGateway:
    provider = "google"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.gemini_model
        self.reasoning_setting = settings.gemini_thinking_level

    @property
    def api_key_configured(self) -> bool:
        return bool(self.settings.gemini_api_key.strip())

    def request_snapshot(self, request: LLMRequest) -> dict[str, object]:
        system_instruction, interaction_input = _interaction_input(request.messages)
        payload: dict[str, object] = {
            "model": self.model,
            "input": interaction_input,
            "stream": True,
            "store": False,
            "generation_config": {
                "thinking_level": self.reasoning_setting,
                "max_output_tokens": self.settings.llm_max_tokens,
            },
        }
        if system_instruction:
            payload["system_instruction"] = system_instruction
        return payload

    async def stream_text(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        api_key = self.settings.gemini_api_key.strip()
        if not api_key:
            raise GatewayError(
                stage="llm",
                provider=self.provider,
                code="api_key_missing",
                public_message="Gemini API key is required.",
                technical_message="GEMINI_API_KEY is not configured.",
            )

        usage: LLMUsage | None = None
        request_id: str | None = None
        response_model: str | None = None
        service_tier: str | None = None
        finish_reason: str | None = None
        timeout = _timeout(self.settings)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.settings.gemini_interactions_url}?alt=sse",
                    headers={
                        "x-goog-api-key": api_key,
                        "Content-Type": "application/json",
                    },
                    json=self.request_snapshot(request),
                ) as response:
                    response.raise_for_status()
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
                            event_type = str(event.get("event_type") or event.get("type") or "")
                            if event_type == "error":
                                raise GatewayError(
                                    stage="llm",
                                    provider=self.provider,
                                    code=str(event.get("code") or "upstream_rejected"),
                                    public_message="Gemini rejected the interaction.",
                                    technical_message=str(event.get("message") or "Gemini stream error."),
                                    retryable=True,
                                    request_id=request_id,
                                )
                            if event_type == "interaction.created":
                                interaction = event.get("interaction") or {}
                                if isinstance(interaction, dict):
                                    request_id = str(interaction.get("id") or request_id or "") or None
                                    response_model = str(interaction.get("model") or response_model or "") or None
                            elif event_type == "step.start":
                                step = event.get("step") or {}
                                if isinstance(step, dict) and step.get("type") == "model_output":
                                    for part in step.get("content") or []:
                                        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                                            yield LLMStreamEvent(kind="delta", text=str(part["text"]))
                            elif event_type == "step.delta":
                                delta = event.get("delta") or {}
                                if isinstance(delta, dict) and delta.get("type") == "text" and delta.get("text"):
                                    yield LLMStreamEvent(kind="delta", text=str(delta["text"]))
                            elif event_type == "interaction.completed":
                                interaction = event.get("interaction") or {}
                                if isinstance(interaction, dict):
                                    request_id = str(interaction.get("id") or request_id or "") or None
                                    response_model = str(interaction.get("model") or response_model or "") or None
                                    service_tier = str(interaction.get("service_tier") or "") or None
                                    finish_reason = str(interaction.get("status") or "completed")
                                    if isinstance(interaction.get("usage"), dict):
                                        usage = _parse_usage(interaction["usage"])
        except GatewayError:
            raise
        except httpx.HTTPStatusError as exc:
            raise _status_error(exc, request_id) from exc
        except (httpx.TimeoutException, TimeoutError, asyncio.TimeoutError) as exc:
            raise GatewayError(
                stage="llm",
                provider=self.provider,
                code="timeout",
                public_message="Gemini timed out.",
                technical_message=str(exc),
                retryable=True,
                request_id=request_id,
            ) from exc
        except httpx.HTTPError as exc:
            raise GatewayError(
                stage="llm",
                provider=self.provider,
                code="transport_error",
                public_message="Gemini is unavailable.",
                technical_message=str(exc),
                retryable=True,
                request_id=request_id,
            ) from exc

        yield LLMStreamEvent(
            kind="complete",
            usage=usage,
            finish_reason=finish_reason,
            request_id=request_id,
            service_tier=service_tier,
            response_model=response_model,
        )


def _interaction_input(messages: list[Message]) -> tuple[str, list[dict[str, object]]]:
    system_parts: list[str] = []
    steps: list[dict[str, object]] = []
    for message in messages:
        role = str(message.get("role") or "")
        text = str(message.get("content") or "")
        if role == "system":
            if text:
                system_parts.append(text)
        elif role == "assistant":
            steps.append({
                "type": "model_output",
                "content": [{"type": "text", "text": text}],
            })
        elif role == "user":
            steps.append({
                "type": "user_input",
                "content": [{"type": "text", "text": text}],
            })
    return "\n\n".join(system_parts), steps


def _parse_usage(payload: dict[str, object]) -> LLMUsage:
    return LLMUsage(
        prompt_tokens=_integer(payload.get("total_input_tokens")),
        completion_tokens=_integer(payload.get("total_output_tokens")),
        total_tokens=_integer(payload.get("total_tokens")),
        cached_tokens=_integer(payload.get("total_cached_tokens")),
        reasoning_tokens=_integer(payload.get("total_thought_tokens")),
    )


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
        provider="google",
        code=code,
        public_message="Gemini rejected the request.",
        technical_message=detail.get("message") or str(exc),
        retryable=status >= 500 or status == 429,
        upstream_status=status,
        request_id=exc.response.headers.get("x-request-id") or request_id,
        provider_detail=detail,
    )
