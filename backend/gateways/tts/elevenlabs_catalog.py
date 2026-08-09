from __future__ import annotations

from urllib.parse import urlparse

import httpx

from ...config import Settings
from ...core.errors import GatewayError, parse_provider_error


class ElevenLabsVoiceCatalog:
    """Small ElevenLabs adapter for the user's saved voice library and previews."""

    provider = "elevenlabs"
    base_url = "https://api.elevenlabs.io"
    max_preview_bytes = 12 * 1024 * 1024

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def list_voices(self, *, api_key: str | None = None) -> list[dict[str, object]]:
        key = self._require_api_key(api_key)
        timeout = self._timeout()
        voices: list[dict[str, object]] = []
        page_token = ""
        async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
            for _ in range(10):
                params: dict[str, object] = {
                    "page_size": 100,
                    "include_total_count": "false",
                    "voice_type": "saved",
                }
                if page_token:
                    params["next_page_token"] = page_token
                response = await client.get(
                    f"{self.base_url}/v2/voices",
                    params=params,
                    headers={"xi-api-key": key},
                )
                self._raise_for_status(response, "Could not load ElevenLabs voices.")
                payload = response.json()
                raw_voices = payload.get("voices", []) if isinstance(payload, dict) else []
                for raw in raw_voices:
                    voice = self._clean_voice(raw)
                    if voice is not None:
                        voices.append(voice)
                if not isinstance(payload, dict) or not payload.get("has_more"):
                    break
                page_token = str(payload.get("next_page_token") or "").strip()
                if not page_token:
                    break

        return _deduplicate_voices(voices)

    async def preview(
        self,
        voice_id: str,
        *,
        api_key: str | None = None,
    ) -> tuple[bytes, str]:
        key = self._require_api_key(api_key)
        selected = voice_id.strip()
        if not selected or len(selected) > 128:
            raise GatewayError(
                stage="tts",
                provider=self.provider,
                code="voice_missing",
                public_message="Choose a valid ElevenLabs voice.",
            )
        async with httpx.AsyncClient(timeout=self._timeout(), transport=self.transport) as client:
            response = await client.get(
                f"{self.base_url}/v1/voices/{selected}",
                headers={"xi-api-key": key},
            )
            self._raise_for_status(response, "Could not load this voice preview.")
            payload = response.json()
            preview_url = str(payload.get("preview_url") or "") if isinstance(payload, dict) else ""
            if not _safe_preview_url(preview_url):
                raise GatewayError(
                    stage="tts",
                    provider=self.provider,
                    code="preview_unavailable",
                    public_message="This voice does not have a preview.",
                )
            audio = await client.get(preview_url)
            self._raise_for_status(audio, "Could not play this voice preview.")
            if len(audio.content) > self.max_preview_bytes:
                raise GatewayError(
                    stage="tts",
                    provider=self.provider,
                    code="preview_too_large",
                    public_message="This voice preview is too large to play.",
                )
            media_type = audio.headers.get("content-type", "audio/mpeg").split(";", 1)[0]
            if not media_type.startswith("audio/"):
                media_type = "audio/mpeg"
            return audio.content, media_type

    def _clean_voice(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        voice_id = str(raw.get("voice_id") or "").strip()
        if not voice_id:
            return None
        # ElevenLabs' `saved` filter also returns community voices that remain
        # callable after prior use. The dashboard's My Voices list is narrower:
        # explicit bookmarks, voices owned by the user, and collection members.
        collection_ids = raw.get("collection_ids")
        is_in_collection = isinstance(collection_ids, list) and bool(collection_ids)
        if raw.get("is_bookmarked") is False and not raw.get("is_owner") and not is_in_collection:
            return None
        labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
        primary_language = _label(labels, "language")
        primary_locale = _label(labels, "locale")
        return {
            "voice_id": voice_id,
            "name": str(raw.get("name") or "Untitled voice").strip(),
            "category": str(raw.get("category") or "").strip(),
            "description": str(raw.get("description") or "").strip(),
            "gender": _label(labels, "gender"),
            "accent": _label(labels, "accent"),
            "age": _label(labels, "age"),
            "style": _label(labels, "descriptive") or _label(labels, "use_case"),
            "primary_language": primary_language,
            "primary_locale": primary_locale,
            "languages": _voice_languages(raw.get("verified_languages"), labels),
            "preview_available": bool(raw.get("preview_url")),
            "is_default": voice_id in {
                self.settings.elevenlabs_male_voice_id,
                self.settings.elevenlabs_female_voice_id,
            },
        }

    def _require_api_key(self, api_key: str | None) -> str:
        key = (api_key or self.settings.elevenlabs_api_key).strip()
        if not key:
            raise GatewayError(
                stage="tts",
                provider=self.provider,
                code="api_key_missing",
                public_message="Add an ElevenLabs API key to load My Voices.",
            )
        return key

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
        status = response.status_code
        detail = parse_provider_error(response)
        if detail.get("status") == "missing_permissions" and "voices_read" in detail.get("message", ""):
            message = "ElevenLabs key needs the voices_read permission to load My Voices."
        code = "authentication_failed" if status == 401 else "authorization_failed" if status == 403 else "upstream_rejected"
        raise GatewayError(
            stage="tts",
            provider=self.provider,
            code=code,
            public_message=message,
            technical_message=f"ElevenLabs voice catalog returned HTTP {status}.",
            retryable=status == 429 or status >= 500,
            upstream_status=status,
            request_id=response.headers.get("request-id") or response.headers.get("x-request-id"),
            provider_detail=detail,
        )


def _label(labels: object, key: str) -> str:
    if not isinstance(labels, dict):
        return ""
    return str(labels.get(key) or "").strip()


def _voice_languages(verified: object, labels: object) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    if isinstance(labels, dict):
        primary = {
            "language": _label(labels, "language"),
            "locale": _label(labels, "locale"),
            "accent": _label(labels, "accent"),
        }
        if primary["language"] or primary["locale"]:
            candidates.append(primary)
    if isinstance(verified, list):
        for item in verified:
            if not isinstance(item, dict):
                continue
            candidate = {
                "language": str(item.get("language") or "").strip(),
                "locale": str(item.get("locale") or "").strip(),
                "accent": str(item.get("accent") or "").strip(),
            }
            if candidate["language"] or candidate["locale"]:
                candidates.append(candidate)

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        key = (candidate["language"], candidate["locale"], candidate["accent"])
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _deduplicate_voices(voices: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for voice in voices:
        voice_id = str(voice.get("voice_id") or "")
        if voice_id in seen:
            continue
        seen.add(voice_id)
        result.append(voice)
    return result


def _safe_preview_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "storage.googleapis.com"
        or host.endswith(".elevenlabs.io")
        or host.endswith(".elevenlabs.net")
    )
