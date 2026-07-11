from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import httpx

from ..config import Settings
from ..core.connectivity import ProviderStatus


class XAIConnectivityProbe:
    provider = "xai"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def check(
        self,
        *,
        api_key: str | None,
        resource_id: str | None = None,
    ) -> ProviderStatus:
        del resource_id
        key = (api_key or self.settings.llm_api_key).strip()
        if not key:
            return _result(self.provider, False, 0, "api_key_missing", "Brain API key is required.")

        started = time.perf_counter_ns()
        url = (
            f"{self.settings.llm_base_url.rstrip('/')}/models/"
            f"{quote(self.settings.llm_model, safe='')}"
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.provider_check_timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.get(url, headers={"Authorization": f"Bearer {key}"})
            if response.status_code >= 400:
                return _http_failure(self.provider, response.status_code, started, "Brain")
            payload = response.json()
            if not isinstance(payload, dict) or not payload.get("id"):
                return _result(
                    self.provider,
                    False,
                    _elapsed_ms(started),
                    "invalid_response",
                    "Brain returned an invalid readiness response.",
                )
            return _result(self.provider, True, _elapsed_ms(started), "ok", "Brain is ready.")
        except (httpx.TimeoutException, asyncio.TimeoutError):
            return _result(
                self.provider,
                False,
                _elapsed_ms(started),
                "timeout",
                "Brain connectivity check timed out.",
            )
        except (httpx.HTTPError, ValueError):
            return _result(
                self.provider,
                False,
                _elapsed_ms(started),
                "transport_error",
                "Brain is unavailable.",
            )


class ElevenLabsConnectivityProbe:
    provider = "elevenlabs"
    base_url = "https://api.elevenlabs.io/v1"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def check(
        self,
        *,
        api_key: str | None,
        resource_id: str | None = None,
    ) -> ProviderStatus:
        del resource_id
        key = (api_key or self.settings.elevenlabs_api_key).strip()
        if not key:
            return _result(self.provider, False, 0, "api_key_missing", "Voice API key is required.")

        started = time.perf_counter_ns()
        headers = {"xi-api-key": key}
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.provider_check_timeout_seconds,
                transport=self.transport,
            ) as client:
                models_response = await client.get(f"{self.base_url}/models", headers=headers)
            if models_response.status_code >= 400:
                return _http_failure(self.provider, models_response.status_code, started, "Voice")

            models = models_response.json()
            model_ids = {
                str(item.get("model_id"))
                for item in models
                if isinstance(item, dict) and item.get("model_id")
            } if isinstance(models, list) else set()
            if self.settings.elevenlabs_tts_model not in model_ids:
                return _result(
                    self.provider,
                    False,
                    _elapsed_ms(started),
                    "model_unavailable",
                    "Configured voice model is unavailable.",
                )
            return _result(self.provider, True, _elapsed_ms(started), "ok", "Voice is ready.")
        except (httpx.TimeoutException, asyncio.TimeoutError):
            return _result(
                self.provider,
                False,
                _elapsed_ms(started),
                "timeout",
                "Voice connectivity check timed out.",
            )
        except (httpx.HTTPError, ValueError):
            return _result(
                self.provider,
                False,
                _elapsed_ms(started),
                "transport_error",
                "Voice is unavailable.",
            )


def _http_failure(provider: str, status: int, started_ns: int, label: str) -> ProviderStatus:
    if status in {401, 403}:
        code = "authentication_failed"
        message = f"{label} API key was rejected."
    elif status == 404:
        code = "resource_unavailable"
        message = f"Configured {label.lower()} resource is unavailable."
    elif status == 429:
        code = "rate_limited"
        message = f"{label} readiness check was rate limited."
    else:
        code = "upstream_rejected"
        message = f"{label} is unavailable."
    return _result(provider, False, _elapsed_ms(started_ns), code, message)


def _result(provider: str, ok: bool, latency_ms: float, code: str, message: str) -> ProviderStatus:
    return ProviderStatus(
        provider=provider,
        ok=ok,
        latency_ms=round(latency_ms, 2),
        code=code,
        message=message,
    )


def _elapsed_ms(started_ns: int) -> float:
    return (time.perf_counter_ns() - started_ns) / 1_000_000
