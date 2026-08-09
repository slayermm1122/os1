from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings
from ..core.errors import GatewayError, parse_provider_error


class ElevenLabsPronunciationGateway:
    provider = "elevenlabs"
    base_url = "https://api.elevenlabs.io/v1/pronunciation-dictionaries"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def get_rules(self, dictionary_id: str | None = None) -> list[dict[str, object]]:
        selected = (dictionary_id or self.settings.elevenlabs_pronunciation_dictionary_id).strip()
        if not selected:
            return []
        async with httpx.AsyncClient(timeout=self._timeout(), transport=self.transport) as client:
            response = await client.get(
                f"{self.base_url}/{selected}",
                headers=self._headers(),
            )
        self._raise_for_status(response, "Could not load the OS1 pronunciation dictionary.")
        payload = response.json()
        raw_rules = payload.get("rules", []) if isinstance(payload, dict) else []
        return [rule for rule in (_clean_alias_rule(item) for item in raw_rules) if rule]

    async def save_rules(self, rules: list[dict[str, object]]) -> tuple[str, str]:
        clean_rules = [rule for rule in (_clean_alias_rule(item) for item in rules) if rule]
        if len(clean_rules) != len(rules):
            raise ValueError("Each pronunciation rule needs text and a spoken alias.")
        dictionary_id = self.settings.elevenlabs_pronunciation_dictionary_id.strip()
        if not clean_rules and not dictionary_id:
            return "", ""
        async with httpx.AsyncClient(timeout=self._timeout(), transport=self.transport) as client:
            if dictionary_id:
                response = await client.post(
                    f"{self.base_url}/{dictionary_id}/set-rules",
                    headers=self._headers(),
                    json={"rules": clean_rules},
                )
            else:
                response = await client.post(
                    f"{self.base_url}/add-from-rules",
                    headers=self._headers(),
                    json={
                        "name": "OS1 pronunciation",
                        "description": "Pronunciation rules managed by the local OS1 app.",
                        "rules": clean_rules,
                    },
                )
        self._raise_for_status(response, "Could not save the OS1 pronunciation dictionary.")
        payload: dict[str, Any] = response.json()
        resolved_dictionary = str(payload.get("id") or dictionary_id).strip()
        version_id = str(payload.get("version_id") or "").strip()
        if not resolved_dictionary or not version_id:
            raise GatewayError(
                stage="tts",
                provider=self.provider,
                code="invalid_dictionary_response",
                public_message="ElevenLabs returned an invalid pronunciation dictionary version.",
            )
        return resolved_dictionary, version_id

    def _headers(self) -> dict[str, str]:
        key = self.settings.elevenlabs_api_key.strip()
        if not key:
            raise GatewayError(
                stage="tts",
                provider=self.provider,
                code="api_key_missing",
                public_message="ElevenLabs API key is required.",
            )
        return {"xi-api-key": key, "Content-Type": "application/json"}

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.settings.upstream_connect_timeout_seconds,
            read=self.settings.upstream_read_timeout_seconds,
            write=self.settings.upstream_write_timeout_seconds,
            pool=self.settings.upstream_pool_timeout_seconds,
        )

    def _raise_for_status(self, response: httpx.Response, message: str) -> None:
        if response.is_success:
            return
        detail = parse_provider_error(response)
        status = response.status_code
        raise GatewayError(
            stage="tts",
            provider=self.provider,
            code=(
                "authentication_failed"
                if status == 401
                else "authorization_failed"
                if status == 403
                else "upstream_rejected"
            ),
            public_message=message,
            technical_message=f"ElevenLabs pronunciation API returned HTTP {status}.",
            retryable=status == 429 or status >= 500,
            upstream_status=status,
            request_id=response.headers.get("request-id") or response.headers.get("x-request-id"),
            provider_detail=detail,
        )


def _clean_alias_rule(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    text = " ".join(str(value.get("string_to_replace") or "").split()).strip()
    alias = " ".join(str(value.get("alias") or "").split()).strip()
    if not text or not alias or value.get("type", "alias") != "alias":
        return None
    return {
        "type": "alias",
        "string_to_replace": text,
        "alias": alias,
        "case_sensitive": False,
        "word_boundaries": True,
    }
