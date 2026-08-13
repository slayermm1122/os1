from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.realtime import create_realtime_router
from backend.config import Settings
from backend.core.live_sessions import LiveSessionRegistry
from backend.core.local_settings import LocalSettingsService
from backend.core.errors import GatewayError
from backend.core.orchestrator import TurnOrchestrator, resolve_response_language
from backend.core.rate_limit import SlidingWindowRateLimiter
from backend.core.sessions import SessionStore
from backend.gateways.pronunciation import ElevenLabsPronunciationGateway
from backend.gateways.llm import LLMRequest, LLMStreamEvent, LLMUsage
from backend.gateways.stt import STTEvent, STTResult
from backend.gateways.stt.elevenlabs import ElevenLabsSTTGateway
from backend.gateways.tts import TTSAdapter, TTSEvent
from backend.gateways.tts.elevenlabs import ElevenLabsTTSGateway
from backend.gateways.tts.elevenlabs_live import ElevenLabsMultiContextSession
from backend.services import ApplicationServices
from backend.telemetry import SQLiteTelemetryRecorder


class ResponseLanguageTests(unittest.TestCase):
    def test_fixed_modes_override_detected_input_language(self) -> None:
        self.assertEqual(resolve_response_language("zh", "en", "en"), "zh")
        self.assertEqual(resolve_response_language("en", "cmn", "zh"), "en")

    def test_auto_normalizes_supported_codes_and_falls_back(self) -> None:
        self.assertEqual(resolve_response_language("auto", "eng"), "en")
        self.assertEqual(resolve_response_language("auto", "cmn"), "zh")
        self.assertEqual(resolve_response_language("auto", "yue"), "zh")
        self.assertEqual(resolve_response_language("auto", None, "zh"), "zh")
        self.assertEqual(resolve_response_language("auto", None, None), "en")


class PronunciationSettingsTests(unittest.TestCase):
    def test_keyterm_limits_and_dictionary_locator_persist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env_path = Path(temp) / ".env"
            settings = Settings()
            service = LocalSettingsService(settings, env_path)

            terms = service.update_stt_keyterms(["OS1", "ElevenLabs", "os1"])
            service.update_pronunciation_locator("dictionary-id", "version-id")

            self.assertEqual(terms, ("OS1", "ElevenLabs"))
            self.assertEqual(settings.elevenlabs_stt_keyterms, terms)
            self.assertEqual(settings.elevenlabs_pronunciation_dictionary_id, "dictionary-id")
            self.assertEqual(settings.elevenlabs_pronunciation_dictionary_version_id, "version-id")
            content = env_path.read_text(encoding="utf-8")
            self.assertIn("ELEVENLABS_STT_KEYTERMS_JSON", content)
            self.assertIn("ELEVENLABS_PRONUNCIATION_DICTIONARY_VERSION_ID=version-id", content)

            with self.assertRaises(ValueError):
                service.update_stt_keyterms(["x" * 21])
            with self.assertRaises(ValueError):
                service.update_stt_keyterms([str(index) for index in range(51)])

    def test_tts_paths_read_the_latest_dictionary_locator(self) -> None:
        settings = Settings(
            elevenlabs_pronunciation_dictionary_id="dictionary-id",
            elevenlabs_pronunciation_dictionary_version_id="version-one",
        )
        gateway = ElevenLabsTTSGateway(settings)
        live = gateway.create_live_session("voice-id")

        self.assertEqual(
            gateway._pronunciation_dictionary_locators(),
            [{
                "pronunciation_dictionary_id": "dictionary-id",
                "version_id": "version-one",
            }],
        )
        settings.elevenlabs_pronunciation_dictionary_version_id = "version-two"
        self.assertEqual(
            live._initial_context_options()["pronunciation_dictionary_locators"][0]["version_id"],
            "version-two",
        )


class PronunciationGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_rules_and_replaces_all_rules(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"rules": [{
                    "type": "alias",
                    "string_to_replace": "OS1",
                    "alias": "O S one",
                    "case_sensitive": True,
                }]})
            return httpx.Response(200, json={
                "id": "dictionary-id",
                "version_id": "version-two",
                "version_rules_num": 1,
            })

        settings = Settings(
            elevenlabs_api_key="secret",
            elevenlabs_pronunciation_dictionary_id="dictionary-id",
            elevenlabs_pronunciation_dictionary_version_id="version-one",
        )
        gateway = ElevenLabsPronunciationGateway(settings, transport=httpx.MockTransport(handler))

        rules = await gateway.get_rules()
        dictionary_id, version_id = await gateway.save_rules(rules)

        self.assertEqual(requests[0].url.path, "/v1/pronunciation-dictionaries/dictionary-id")
        self.assertEqual(
            requests[1].url.path,
            "/v1/pronunciation-dictionaries/dictionary-id/set-rules",
        )
        sent = json.loads(requests[1].content)
        self.assertFalse(sent["rules"][0]["case_sensitive"])
        self.assertTrue(sent["rules"][0]["word_boundaries"])
        self.assertEqual((dictionary_id, version_id), ("dictionary-id", "version-two"))

    async def test_creates_named_dictionary_and_skips_empty_initial_resource(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "new-id", "version_id": "version-one"})

        gateway = ElevenLabsPronunciationGateway(
            Settings(elevenlabs_api_key="secret"),
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(await gateway.save_rules([]), ("", ""))
        result = await gateway.save_rules([{
            "type": "alias",
            "string_to_replace": "OS1",
            "alias": "O S one",
        }])

        self.assertEqual(result, ("new-id", "version-one"))
        self.assertEqual(len(requests), 1)
        body = json.loads(requests[0].content)
        self.assertEqual(body["name"], "OS1 pronunciation")


class FakeMultiContextWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.response = type("Response", (), {"headers": {"request-id": "request-one"}})()

    async def send(self, value: str) -> None:
        self.sent.append(json.loads(value))

    async def close(self) -> None:
        await self.incoming.put(None)

    async def __aiter__(self):
        while True:
            value = await self.incoming.get()
            if value is None:
                return
            yield value


class MultiContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_audio_arrives_before_later_text_and_socket_is_reused(self) -> None:
        websocket = FakeMultiContextWebSocket()
        connect = AsyncMock(return_value=websocket)
        settings = Settings(
            elevenlabs_api_key="secret",
            elevenlabs_stream_output_format="pcm_16000",
        )
        session = ElevenLabsMultiContextSession(settings, "voice-id")

        allow_second_chunk = asyncio.Event()
        first_audio_received = asyncio.Event()

        async def chunks():
            yield "First sentence."
            await allow_second_chunk.wait()
            yield "Second sentence."

        async def consume():
            events = []
            async for event in session.stream_context(chunks(), context_id="turn-one"):
                events.append(event)
                if event.kind == "audio":
                    first_audio_received.set()
            return events

        with patch("backend.gateways.tts.elevenlabs_live.websockets.connect", connect):
            task = asyncio.create_task(consume())
            for _ in range(20):
                if any(item.get("context_id") == "turn-one" for item in websocket.sent):
                    break
                await asyncio.sleep(0)
            await websocket.incoming.put(json.dumps({
                "contextId": "turn-one",
                "audio": base64.b64encode(b"first-pcm").decode("ascii"),
            }))
            await asyncio.wait_for(first_audio_received.wait(), timeout=1)
            turn_messages = [item for item in websocket.sent if item.get("context_id") == "turn-one"]
            self.assertEqual(len(turn_messages), 1)

            allow_second_chunk.set()
            for _ in range(20):
                turn_messages = [item for item in websocket.sent if item.get("context_id") == "turn-one"]
                if len(turn_messages) >= 2:
                    break
                await asyncio.sleep(0)
            await websocket.incoming.put(json.dumps({
                "contextId": "turn-one",
                "audio": base64.b64encode(b"second-pcm").decode("ascii"),
            }))
            await websocket.incoming.put(json.dumps({"contextId": "turn-one", "isFinal": True}))
            events = await asyncio.wait_for(task, timeout=1)
            await session.close()

        self.assertEqual(connect.await_count, 1)
        self.assertEqual([event.audio for event in events if event.kind == "audio"], [
            b"first-pcm",
            b"second-pcm",
        ])
        turn_messages = [item for item in websocket.sent if item.get("context_id") == "turn-one"]
        self.assertEqual([item.get("flush") for item in turn_messages[:2]], [True, True])
        self.assertTrue(turn_messages[-1].get("close_context"))
        self.assertTrue(any(item.get("context_id") == session.KEEPALIVE_CONTEXT for item in websocket.sent))


class FakeRealtimeSTTWebSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = messages
        self.sent: list[dict[str, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def send(self, value: str) -> None:
        self.sent.append(json.loads(value))

    async def __aiter__(self):
        for message in self.messages:
            await asyncio.sleep(0)
            yield json.dumps(message)


class ContinuousScribeTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_scribe_receives_repeated_keyterms(self) -> None:
        captured = b""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured
            captured = request.content
            return httpx.Response(200, json={"text": "OS1", "language_code": "eng"})

        original_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        gateway = ElevenLabsSTTGateway(Settings(
            elevenlabs_api_key="secret",
            elevenlabs_stt_keyterms=("OS1", "ElevenLabs"),
        ))
        with patch("backend.gateways.stt.elevenlabs.httpx.AsyncClient", client_factory):
            result = await gateway.transcribe_upload(
                filename="speech.wav",
                content_type="audio/wav",
                data=b"wave",
            )

        body = captured.decode("utf-8", errors="replace")
        self.assertEqual(result.text, "OS1")
        self.assertEqual(body.count('name="keyterms"'), 2)
        self.assertIn("OS1", body)
        self.assertIn("ElevenLabs", body)

    async def test_one_connection_emits_multiple_vad_commits(self) -> None:
        websocket = FakeRealtimeSTTWebSocket([
            {"message_type": "session_started", "session_id": "scribe-session"},
            {"message_type": "partial_transcript", "text": "hel"},
            {"message_type": "committed_transcript", "text": "hello"},
            {"message_type": "partial_transcript", "text": "再"},
            {"message_type": "committed_transcript", "text": "再见"},
        ])
        captured_uri = ""

        def connect(uri: str, **_kwargs):
            nonlocal captured_uri
            captured_uri = uri
            return websocket

        async def audio():
            yield b"pcm"

        gateway = ElevenLabsSTTGateway(Settings(elevenlabs_api_key="secret"))
        events = []
        with patch("backend.gateways.stt.elevenlabs.websockets.connect", connect):
            with self.assertRaisesRegex(GatewayError, "disconnected"):
                async for event in gateway.stream_realtime(
                    audio(),
                    sample_rate=16000,
                    continuous=True,
                    filter_background_audio=True,
                    keyterms=("OS1", "ElevenLabs"),
                ):
                    events.append(event)

        self.assertEqual(
            [event.text for event in events if event.kind == "committed"],
            ["hello", "再见"],
        )
        query = parse_qs(urlparse(captured_uri).query)
        self.assertEqual(query["include_timestamps"], ["false"])
        self.assertEqual(query["filter_background_audio"], ["true"])
        self.assertEqual(query["secondary_languages"], ["en", "zh"])
        self.assertEqual(query["keyterms"], ["OS1", "ElevenLabs"])
        self.assertNotIn("language_code", query)

    async def test_auto_waits_for_delayed_language_result_without_timestamps(self) -> None:
        websocket = FakeRealtimeSTTWebSocket([
            {"message_type": "session_started"},
            {"message_type": "committed_transcript", "text": "你好"},
            {
                "message_type": "final_transcript_with_timestamps",
                "text": "你好",
                "language_code": "cmn",
                "words": [],
            },
        ])

        async def audio():
            yield b"pcm"

        gateway = ElevenLabsSTTGateway(Settings(elevenlabs_api_key="secret"))
        events = []
        with patch(
            "backend.gateways.stt.elevenlabs.websockets.connect",
            lambda *_args, **_kwargs: websocket,
        ):
            with self.assertRaisesRegex(GatewayError, "disconnected"):
                async for event in gateway.stream_realtime(
                    audio(),
                    sample_rate=16000,
                    continuous=True,
                    detect_language=True,
                    filter_background_audio=True,
                ):
                    events.append(event)

        commits = [event for event in events if event.kind == "committed"]
        self.assertEqual([(event.text, event.language_code) for event in commits], [("你好", "cmn")])


class LiveLLM:
    provider = "live-llm"
    model = "live-model"
    reasoning_effort = "low"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def request_snapshot(self, request: LLMRequest) -> dict[str, object]:
        return {"messages": request.messages, "model": self.model}

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.requests.append(request)
        yield LLMStreamEvent(kind="delta", text="A live answer.")
        yield LLMStreamEvent(
            kind="complete",
            usage=LLMUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
            finish_reason="stop",
        )


class ContinuousFakeSTT:
    provider = "fake-stt"
    upload_model = "fake-upload"
    realtime_model = "fake-realtime"

    def __init__(self) -> None:
        self.connections = 0
        self.turn = 0

    async def transcribe_upload(self, **_kwargs) -> STTResult:
        return STTResult(text="unused")

    async def stream_realtime(self, audio_chunks, **_kwargs):
        self.connections += 1
        yield STTEvent(kind="session_started", request_id="scribe-session")
        async for _chunk in audio_chunks:
            self.turn += 1
            text = f"utterance {self.turn}"
            yield STTEvent(kind="partial", text=text[:5])
            yield STTEvent(kind="committed", text=text, language_code="en")


class FakeLiveTTSConnection:
    def __init__(self) -> None:
        self.connected = 0
        self.contexts: list[str] = []
        self.closed_contexts: list[str] = []
        self.closed = False

    async def connect(self) -> None:
        self.connected += 1

    async def stream_context(self, text_chunks, *, context_id: str):
        self.contexts.append(context_id)
        async for _chunk in text_chunks:
            yield TTSEvent(kind="audio", audio=b"\x00\x00" * 80)
        yield TTSEvent(kind="complete")

    async def close_context(self, context_id: str) -> None:
        self.closed_contexts.append(context_id)

    async def close(self) -> None:
        self.closed = True


class LiveTTSGateway:
    provider = "elevenlabs"
    model = "eleven_flash_v2_5"
    http_output_format = "pcm_16000"
    stream_output_format = "pcm_16000"
    stream_sample_rate = 16000
    http_media_type = "audio/L16"
    stream_media_type = "audio/L16"
    api_key_configured = True

    def __init__(self, connection: FakeLiveTTSConnection) -> None:
        self.connection = connection

    def resolve_voice_id(self, voice_id, _voice_gender):
        return voice_id

    def create_live_session(self, _voice_id=None):
        return self.connection

    async def stream_http(self, _text, **_kwargs):
        yield TTSEvent(kind="audio", audio=b"pcm")

    async def stream_websocket(self, text_chunks, **_kwargs):
        async for _chunk in text_chunks:
            pass
        yield TTSEvent(kind="complete")


class LiveSessionIntegrationTests(unittest.TestCase):
    def test_one_session_socket_handles_two_turns_and_commits_playback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            telemetry = SQLiteTelemetryRecorder(Path(temp) / "telemetry.sqlite")
            settings = Settings(
                enforce_local_access=False,
                assistant_response_language="en",
                elevenlabs_voice_id="voice-id",
                assistant_name="Sophie",
                assistant_name_pronunciation="so fee",
                user_name="Moi",
                user_name_pronunciation="mou e",
                assistant_persona="empathetic",
            )
            stt = ContinuousFakeSTT()
            live_tts = FakeLiveTTSConnection()
            gateway = LiveTTSGateway(live_tts)
            sessions = SessionStore(max_turns=10, max_sessions=10, ttl_seconds=3600)
            llm = LiveLLM()
            orchestrator = TurnOrchestrator(
                settings=settings,
                llm=llm,
                stt=stt,
                tts=TTSAdapter([gateway], default_provider="elevenlabs"),
                sessions=sessions,
                telemetry=telemetry,
            )
            services = ApplicationServices(
                settings=settings,
                orchestrator=orchestrator,
                telemetry=telemetry,
                rate_limiter=SlidingWindowRateLimiter(requests=20, window_seconds=60),
                live_sessions=LiveSessionRegistry(),
            )

            @asynccontextmanager
            async def lifespan(_app: FastAPI):
                await telemetry.start()
                yield
                await services.live_sessions.close_all()
                await telemetry.close()

            app = FastAPI(lifespan=lifespan)
            app.include_router(create_realtime_router(services))
            received: list[str] = []
            with TestClient(app) as client:
                with client.websocket_connect("/api/realtime/session") as websocket:
                    websocket.send_json({
                        "type": "session_start",
                        "session_id": "live-session",
                        "sample_rate": 16000,
                    })
                    ready = websocket.receive_json()
                    self.assertEqual(ready["event"], "live_ready")
                    self.assertEqual(
                        ready["data"]["persona"],
                        {
                            "assistant_name": "Sophie",
                            "assistant_name_pronunciation": "so fee",
                            "user_name": "Moi",
                            "user_name_pronunciation": "mou e",
                            "persona": "empathetic",
                        },
                    )
                    settings.assistant_name = "Changed"
                    settings.user_name = "Different"
                    settings.assistant_persona = "concise"
                    websocket.send_json({"type": "listen_start"})
                    while "stt_ready" not in received:
                        received.append(websocket.receive_json()["event"])

                    for turn_number in range(2):
                        websocket.send_bytes(b"\x00\x00" * 80)
                        turn_id = ""
                        saw_done = False
                        saw_audio_done = False
                        while not (saw_done and saw_audio_done):
                            payload = websocket.receive_json()
                            received.append(payload["event"])
                            if payload["event"] == "barge_in":
                                websocket.send_json({
                                    "type": "client_event",
                                    "name": "browser.playback_interrupted",
                                    "turn_id": payload["data"]["turn_id"],
                                    "spoken_text": "A live",
                                })
                            turn_id = str(payload.get("data", {}).get("turn_id") or turn_id)
                            saw_done = saw_done or payload["event"] == "done"
                            saw_audio_done = saw_audio_done or payload["event"] == "audio_done"
                        if turn_number == 1:
                            websocket.send_json({
                                "type": "client_event",
                                "name": "browser.playback_ended",
                                "turn_id": turn_id,
                            })
                    websocket.send_json({"type": "close_session"})
                    self.assertEqual(websocket.receive_json()["event"], "session_closed")

            self.assertEqual(stt.connections, 1)
            self.assertEqual(live_tts.connected, 1)
            self.assertEqual(len(live_tts.contexts), 2)
            self.assertIn("barge_in", received)
            self.assertIn(live_tts.contexts[0], live_tts.closed_contexts)
            self.assertTrue(live_tts.closed)
            history = sessions.get_history("live-session")
            self.assertEqual(len(history), 4)
            self.assertEqual(history[0]["content"], "utterance 1\n\nPlease respond in English.")
            self.assertEqual(history[1]["content"], "A live")
            self.assertEqual(history[2]["content"], "utterance 2\n\nPlease respond in English.")
            self.assertEqual(len(llm.requests), 2)
            for request in llm.requests:
                system_prompt = request.messages[0]["content"]
                self.assertIn('Your name is "Sophie"', system_prompt)
                self.assertIn('user\'s name is "Moi"', system_prompt)
                self.assertIn("empathetic AI companion", system_prompt)
                self.assertNotIn('Your name is "Changed"', system_prompt)


if __name__ == "__main__":
    unittest.main()
