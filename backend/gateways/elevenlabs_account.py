from __future__ import annotations

import httpx

from ..config import Settings
from ..core.errors import GatewayError, parse_provider_error


class ElevenLabsAccountGateway:
    """Returns only the small, non-secret account summary used by the sidebar."""

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

    async def summary(self) -> dict[str, object]:
        key = self.settings.elevenlabs_api_key.strip()
        if not key:
            raise GatewayError(
                stage="account",
                provider=self.provider,
                code="api_key_missing",
                public_message="ELEVENLABS_API_KEY is not configured in .env.",
            )
        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=self.settings.upstream_read_timeout_seconds,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )
        async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
            response = await client.get(
                f"{self.base_url}/user",
                headers={"xi-api-key": key},
            )
        if not response.is_success:
            detail = parse_provider_error(response)
            message = "Could not load ElevenLabs account information."
            if detail.get("status") == "missing_permissions" and "user_read" in detail.get("message", ""):
                message = "ElevenLabs key needs the user_read permission to show account balance."
            raise GatewayError(
                stage="account",
                provider=self.provider,
                code="authentication_failed" if response.status_code == 401 else "upstream_rejected",
                public_message=message,
                technical_message=f"ElevenLabs user endpoint returned HTTP {response.status_code}.",
                retryable=response.status_code == 429 or response.status_code >= 500,
                upstream_status=response.status_code,
                request_id=response.headers.get("request-id") or response.headers.get("x-request-id"),
                provider_detail=detail,
            )
        payload = response.json()
        subscription = payload.get("subscription") if isinstance(payload, dict) else {}
        if not isinstance(subscription, dict):
            subscription = {}
        used = _integer(subscription.get("character_count"))
        limit = _integer(subscription.get("character_limit"))
        tier = str(subscription.get("tier") or "")
        first_name = str(payload.get("first_name") or "").strip() if isinstance(payload, dict) else ""
        return {
            "provider": self.provider,
            "name": first_name or tier.replace("_", " ").title() or "ElevenLabs",
            "tier": tier,
            "status": str(subscription.get("status") or ""),
            "credits_used": used,
            "credits_limit": limit,
            "credits_remaining": max(limit - used, 0),
            "next_reset_unix": _integer(subscription.get("next_character_count_reset_unix")),
        }


def _integer(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
