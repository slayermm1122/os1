from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

import httpx

from backend.config import Settings
from backend.core.captions import CaptionAlignment, caption_payload
from backend.core.local_settings import LocalSettingsService
from backend.gateways.elevenlabs_account import ElevenLabsAccountGateway
from backend.gateways.tts.elevenlabs_catalog import ElevenLabsVoiceCatalog


class ElevenLabsVoiceCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_clean_deduplicated_my_voices(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v2/voices")
            self.assertEqual(request.headers["xi-api-key"], "browser-key")
            self.assertEqual(request.url.params["voice_type"], "saved")
            return httpx.Response(
                200,
                json={
                    "voices": [
                        {
                            "voice_id": "voice-one",
                            "name": "Calm voice",
                            "is_bookmarked": True,
                            "category": "premade",
                            "description": "Long description is kept out of the UI metadata.",
                            "labels": {
                                "gender": "female",
                                "accent": "american",
                                "descriptive": "calm",
                                "language": "en",
                                "locale": "en-US",
                            },
                            "verified_languages": [
                                {"language": "en", "locale": "en-US", "accent": "american"},
                                {"language": "es", "locale": "es-ES", "accent": "castilian"},
                            ],
                            "preview_url": "https://storage.googleapis.com/example.mp3",
                        },
                        {
                            "voice_id": "recently-used",
                            "name": "Callable but not saved",
                            "is_bookmarked": False,
                            "is_owner": False,
                            "collection_ids": [],
                        },
                        {
                            "voice_id": "owned-voice",
                            "name": "My clone",
                            "is_bookmarked": False,
                            "is_owner": True,
                        },
                        {"voice_id": "voice-one", "name": "Duplicate"},
                    ],
                    "has_more": False,
                },
            )

        catalog = ElevenLabsVoiceCatalog(
            Settings(elevenlabs_api_key="server-key"),
            transport=httpx.MockTransport(handler),
        )
        voices = await catalog.list_voices(api_key="browser-key")

        self.assertEqual(len(voices), 2)
        self.assertEqual(voices[0]["voice_id"], "voice-one")
        self.assertEqual(voices[0]["gender"], "female")
        self.assertEqual(voices[0]["style"], "calm")
        self.assertTrue(voices[0]["preview_available"])
        self.assertEqual(voices[0]["primary_language"], "en")
        self.assertEqual(
            [language["language"] for language in voices[0]["languages"]],
            ["en", "es"],
        )
        self.assertEqual(voices[1]["voice_id"], "owned-voice")

    async def test_uses_next_page_token_for_voice_pagination(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    json={"voices": [], "has_more": True, "next_page_token": "page-two"},
                )
            self.assertEqual(request.url.params["next_page_token"], "page-two")
            self.assertNotIn("page_token", request.url.params)
            return httpx.Response(200, json={"voices": [], "has_more": False})

        catalog = ElevenLabsVoiceCatalog(
            Settings(elevenlabs_api_key="server-key"),
            transport=httpx.MockTransport(handler),
        )
        await catalog.list_voices()

        self.assertEqual(len(requests), 2)

    async def test_proxies_only_trusted_audio_preview_urls(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/voices/voice-one":
                return httpx.Response(
                    200,
                    json={
                        "preview_url": "https://storage.googleapis.com/eleven-public/voice.mp3"
                    },
                )
            self.assertEqual(request.url.host, "storage.googleapis.com")
            return httpx.Response(200, content=b"audio", headers={"content-type": "audio/mpeg"})

        catalog = ElevenLabsVoiceCatalog(
            Settings(elevenlabs_api_key="server-key"),
            transport=httpx.MockTransport(handler),
        )
        audio, media_type = await catalog.preview("voice-one")

        self.assertEqual(audio, b"audio")
        self.assertEqual(media_type, "audio/mpeg")


class CaptionModuleTests(unittest.TestCase):
    def test_alignment_transport_is_provider_neutral(self) -> None:
        payload = caption_payload(
            CaptionAlignment(
                chars=("O", "S", "1"),
                char_start_times_ms=(0.0, 50.0, 100.0),
                char_durations_ms=(50.0, 50.0, 80.0),
            )
        )
        self.assertEqual(payload["chars"], ["O", "S", "1"])
        self.assertEqual(payload["char_start_times_ms"], [0.0, 50.0, 100.0])


class ElevenLabsAccountGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_only_sidebar_account_fields_and_remaining_credits(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/user")
            return httpx.Response(
                200,
                json={
                    "user_id": "private-user-id",
                    "first_name": "Maya",
                    "xi_api_key": "must-never-be-returned",
                    "subscription": {
                        "tier": "creator",
                        "status": "active",
                        "character_count": 12_500,
                        "character_limit": 100_000,
                        "next_character_count_reset_unix": 1_800_000_000,
                    },
                },
            )

        gateway = ElevenLabsAccountGateway(
            Settings(elevenlabs_api_key="server-key"),
            transport=httpx.MockTransport(handler),
        )
        summary = await gateway.summary()

        self.assertEqual(summary["name"], "Maya")
        self.assertEqual(summary["credits_remaining"], 87_500)
        self.assertEqual(summary["tier"], "creator")
        self.assertNotIn("user_id", summary)
        self.assertNotIn("xi_api_key", summary)


class LocalSettingsServiceTests(unittest.TestCase):
    def test_voice_and_profile_updates_are_written_to_env_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env_path = Path(temp) / ".env"
            settings = Settings(elevenlabs_voice_id="old-voice")
            service = LocalSettingsService(settings, env_path)

            service.select_voice("new_voice_123", "zh")
            service.update_persona(
                assistant_name="Samantha",
                user_name="小明",
                persona="conversational",
            )
            service.update_voice_profile(
                vad_silence_threshold_secs=1.4,
                vad_threshold=0.35,
            )

            content = env_path.read_text(encoding="utf-8")
            self.assertIn("ELEVENLABS_VOICE_ID=new_voice_123", content)
            self.assertIn("ELEVENLABS_VOICE_LANGUAGE=zh", content)
            self.assertIn("ASSISTANT_NAME=Samantha", content)
            self.assertIn("USER_NAME=小明", content)
            self.assertIn("ASSISTANT_PERSONA=conversational", content)
            self.assertEqual(settings.elevenlabs_voice_id, "new_voice_123")
            self.assertEqual(settings.elevenlabs_voice_language, "zh")
            self.assertEqual(settings.assistant_name, "Samantha")
            self.assertEqual(settings.user_name, "小明")
            self.assertEqual(settings.assistant_persona, "conversational")
            self.assertEqual(settings.elevenlabs_stt_vad_threshold, 0.35)
