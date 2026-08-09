from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sqlite3
import tempfile
import unittest
import warnings
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path

import httpx

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.realtime import create_realtime_router
from backend.api.routes import create_router
from backend.config import Settings
from backend.core.chunking import pop_ready_speech_chunks
from backend.core.connectivity import ConnectivityService, ProviderStatus
from backend.core.errors import GatewayError, error_info, parse_provider_error, redact
from backend.core.orchestrator import TurnOrchestrator
from backend.core.rate_limit import SlidingWindowRateLimiter
from backend.core.security import is_allowed_websocket, is_local_http_request
from backend.core.sessions import SessionStore
from backend.gateways.connectivity import (
    DeepSeekConnectivityProbe,
    ElevenLabsConnectivityProbe,
    GeminiConnectivityProbe,
    XAIConnectivityProbe,
)
from backend.gateways.llm import AIAdapter, LLMRequest, LLMStreamEvent, LLMUsage
from backend.gateways.llm.deepseek import DeepSeekLLMGateway
from backend.gateways.llm.deepseek import _parse_usage as parse_deepseek_usage
from backend.gateways.llm.gemini import GeminiLLMGateway, _interaction_input
from backend.gateways.llm.gemini import _parse_usage as parse_gemini_usage
from backend.gateways.llm.xai import _parse_usage as parse_xai_usage
from backend.gateways.stt import STTEvent, STTResult, STTWordTiming
from backend.gateways.stt.elevenlabs import (
    _gateway_error as stt_gateway_error,
    _parse_word_timings,
)
from backend.gateways.tts import TTSAdapter, TTSAlignment, TTSEvent
from backend.gateways.tts.elevenlabs import _parse_alignment
from backend.telemetry import SQLiteTelemetryRecorder
from backend.services import ApplicationServices


class FakeLLM:
    provider = "fake_llm"
    model = "fake-fast"
    reasoning_effort = "low"

    def request_snapshot(self, request: LLMRequest) -> dict[str, object]:
        return {
            "model": self.model,
            "messages": request.messages,
            "reasoning_effort": self.reasoning_effort,
            "stream": True,
        }

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        yield LLMStreamEvent(kind="delta", text="A concise answer.")
        yield LLMStreamEvent(
            kind="complete",
            usage=LLMUsage(
                prompt_tokens=20,
                completion_tokens=8,
                total_tokens=28,
                cached_tokens=10,
                reasoning_tokens=2,
                cost_usd_ticks=1234,
            ),
            finish_reason="stop",
            request_id="llm-request",
            system_fingerprint="fingerprint",
            service_tier="default",
            response_model="fake-returned",
        )


class EmptyLLM(FakeLLM):
    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        yield LLMStreamEvent(
            kind="complete",
            usage=LLMUsage(
                prompt_tokens=12,
                completion_tokens=0,
                total_tokens=12,
                cached_tokens=0,
                reasoning_tokens=3,
                cost_usd_ticks=456,
            ),
            finish_reason="stop",
            request_id="empty-llm-request",
        )


class FailingLLM(FakeLLM):
    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        raise GatewayError(
            stage="llm",
            provider=self.provider,
            code="provider_failure",
            public_message="Brain failed.",
            technical_message="synthetic llm failure",
        )
        yield LLMStreamEvent(kind="complete")


class FakeProbe:
    def __init__(self, provider: str, *, ok: bool = True) -> None:
        self.provider = provider
        self.ok = ok
        self.calls: list[tuple[str | None, str | None]] = []

    async def check(
        self,
        *,
        api_key: str | None,
        resource_id: str | None = None,
    ) -> ProviderStatus:
        self.calls.append((api_key, resource_id))
        return ProviderStatus(
            provider=self.provider,
            ok=self.ok,
            latency_ms=12.5,
            code="ok" if self.ok else "transport_error",
            message=f"{self.provider} {'ready' if self.ok else 'unavailable'}.",
        )


class FakeSTT:
    provider = "fake_stt"
    upload_model = "fake-upload"
    realtime_model = "fake-realtime"

    def __init__(self) -> None:
        self.api_keys: list[str | None] = []

    async def transcribe_upload(self, **kwargs) -> STTResult:
        self.api_keys.append(kwargs.get("api_key"))
        return STTResult(text="hello", raw={"text": "hello"}, request_id="stt-upload")

    async def stream_realtime(
        self,
        audio_chunks: AsyncIterable[bytes],
        *,
        sample_rate: int,
        api_key: str | None = None,
        vad_threshold: float | None = None,
        vad_silence_threshold_secs: float | None = None,
    ) -> AsyncIterator[STTEvent]:
        self.api_keys.append(api_key)
        yield STTEvent(kind="session_started", request_id="stt-session")
        async for _ in audio_chunks:
            pass
        yield STTEvent(kind="partial", text="hel", request_id="stt-session")
        yield STTEvent(kind="committed", text="hello", language_code="en", request_id="stt-session")
        yield STTEvent(
            kind="timing",
            text="hello",
            language_code="en",
            request_id="stt-session",
            words=(STTWordTiming(text="hello", start_ms=0, end_ms=480),),
        )


class SlowSTT(FakeSTT):
    async def stream_realtime(self, *args, **kwargs) -> AsyncIterator[STTEvent]:
        await asyncio.Event().wait()
        yield STTEvent(kind="committed", text="unreachable")


class FailingSTT(FakeSTT):
    async def stream_realtime(self, *args, **kwargs) -> AsyncIterator[STTEvent]:
        raise GatewayError(
            stage="stt",
            provider=self.provider,
            code="provider_failure",
            public_message="Transcription failed.",
            technical_message="synthetic stt failure",
        )
        yield STTEvent(kind="committed", text="unreachable")


class FakeTTS:
    provider = "fake_tts"
    model = "fake-voice"
    http_output_format = "pcm_16000"
    stream_output_format = "pcm_16000"
    stream_sample_rate = 16000
    http_media_type = "audio/L16"
    stream_media_type = "audio/L16"
    api_key_configured = True

    def __init__(self) -> None:
        self.http_texts: list[str] = []
        self.websocket_api_keys: list[str | None] = []

    def resolve_voice_id(self, voice_id: str | None, voice_gender: str | None) -> str | None:
        return voice_id or (f"fake-{voice_gender}" if voice_gender else None)

    async def stream_http(self, text: str, **kwargs) -> AsyncIterator[TTSEvent]:
        self.http_texts.append(text)
        yield TTSEvent(kind="audio", audio=b"\x00\x00" * 160)
        yield TTSEvent(kind="complete", request_id="tts-http", character_cost=len(text))

    async def stream_websocket(
        self,
        text_chunks: AsyncIterable[str],
        **kwargs,
    ) -> AsyncIterator[TTSEvent]:
        self.websocket_api_keys.append(kwargs.get("api_key"))
        async for _ in text_chunks:
            pass
        text = "A concise answer."
        yield TTSEvent(
            kind="audio",
            audio=b"\x00\x00" * 1600,
            alignment=TTSAlignment(
                chars=tuple(text),
                char_start_times_ms=tuple(index * 5 for index in range(len(text))),
                char_durations_ms=tuple(5 for _ in text),
            ),
        )
        yield TTSEvent(kind="complete", request_id="tts-ws")


class FailingTTS(FakeTTS):
    async def stream_websocket(self, *args, **kwargs) -> AsyncIterator[TTSEvent]:
        raise GatewayError(
            stage="tts",
            provider=self.provider,
            code="provider_failure",
            public_message="Voice failed.",
            technical_message="synthetic failure",
            upstream_status=402,
            request_id="provider-request-id",
            provider_detail={
                "type": "payment_required",
                "status": "quota_exceeded",
                "message": "Synthetic quota exhausted.",
            },
        )
        yield TTSEvent(kind="complete")


class EmptyTTS(FakeTTS):
    async def stream_websocket(
        self,
        text_chunks: AsyncIterable[str],
        **kwargs,
    ) -> AsyncIterator[TTSEvent]:
        async for _ in text_chunks:
            pass
        yield TTSEvent(kind="complete", request_id="empty-tts")


async def audio() -> AsyncIterator[bytes]:
    yield b"\x00\x00" * 1600


class ElevenLabsTimingParsingTests(unittest.TestCase):
    def test_stt_word_timings_are_normalized_to_milliseconds(self) -> None:
        words = _parse_word_timings(
            [
                {"text": "Hello", "start": 0, "end": 0.42, "type": "word"},
                {"text": " ", "start": 0.42, "end": 0.44, "type": "spacing"},
                {"text": "ignored", "start": "bad", "end": 1},
            ]
        )

        self.assertEqual(len(words), 2)
        self.assertEqual(words[0], STTWordTiming("Hello", 0, 420, "word"))
        self.assertEqual(words[1], STTWordTiming(" ", 420, 440, "spacing"))

    def test_tts_alignment_rejects_bad_entries_without_losing_valid_cues(self) -> None:
        alignment = _parse_alignment(
            {
                "chars": ["你", "好", "x"],
                "charStartTimesMs": [0, 80, "bad"],
                "charDurationsMs": [80, 120, 40],
            }
        )

        self.assertIsNotNone(alignment)
        self.assertEqual(alignment.chars, ("你", "好"))
        self.assertEqual(alignment.char_start_times_ms, (0.0, 80.0))
        self.assertEqual(alignment.char_durations_ms, (80.0, 120.0))


class TTSAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_to_the_requested_provider_and_rejects_unknown_providers(self) -> None:
        primary = FakeTTS()
        alternate = FakeTTS()
        alternate.provider = "alternate_tts"
        alternate.model = "alternate-voice"
        adapter = TTSAdapter(
            [primary, alternate],
            default_provider=primary.provider,
        )

        self.assertIs(adapter.resolve(), primary)
        self.assertIs(adapter.resolve("ALTERNATE_TTS"), alternate)
        self.assertEqual(adapter.providers, ("fake_tts", "alternate_tts"))
        events = [
            event
            async for event in adapter.stream_http(
                "speak through alternate",
                provider="alternate_tts",
            )
        ]
        self.assertEqual(alternate.http_texts, ["speak through alternate"])
        self.assertEqual([event.kind for event in events], ["audio", "complete"])
        self.assertEqual(primary.http_texts, [])
        with self.assertRaises(GatewayError) as raised:
            adapter.resolve("missing")
        self.assertEqual(raised.exception.code, "unsupported_provider")


class LLMGatewayContractTests(unittest.TestCase):
    def test_adapter_selects_provider_and_reports_model_configuration(self) -> None:
        settings = Settings(
            llm_api_key="xai-key",
            deepseek_api_key="deepseek-key",
            gemini_api_key="gemini-key",
        )
        xai = FakeLLM()
        xai.provider = "xai"
        xai.api_key_configured = True
        xai.reasoning_setting = "low"
        deepseek = DeepSeekLLMGateway(settings)
        gemini = GeminiLLMGateway(settings)
        adapter = AIAdapter([xai, deepseek, gemini], default_provider="xai")

        self.assertIs(adapter.resolve(), xai)
        adapter.select("deepseek")
        self.assertIs(adapter.resolve(), deepseek)
        self.assertEqual([item["provider"] for item in adapter.catalog()], ["xai", "deepseek", "google"])
        self.assertEqual(adapter.catalog()[1]["reasoning_setting"], "disabled")
        self.assertEqual(adapter.catalog()[2]["reasoning_setting"], "minimal")

    def test_deepseek_request_and_usage_follow_v4_contract(self) -> None:
        gateway = DeepSeekLLMGateway(Settings(deepseek_api_key="key"))
        request = LLMRequest(
            messages=[{"role": "user", "content": "Hello"}],
            cache_key="session/one",
        )
        payload = gateway.request_snapshot(request)

        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(payload["user_id"], "session_one")
        usage = parse_deepseek_usage({
            "prompt_tokens": 120,
            "prompt_cache_hit_tokens": 90,
            "prompt_cache_miss_tokens": 30,
            "completion_tokens": 14,
            "total_tokens": 134,
            "completion_tokens_details": {"reasoning_tokens": 0},
        })
        self.assertEqual(usage.prompt_tokens, 120)
        self.assertEqual(usage.cached_tokens, 90)
        self.assertEqual(usage.reasoning_tokens, 0)
        self.assertEqual(usage.cache_status, "partial")

    def test_gemini_interactions_request_and_usage_follow_latest_contract(self) -> None:
        gateway = GeminiLLMGateway(Settings(gemini_api_key="key"))
        request = LLMRequest(messages=[
            {"role": "system", "content": "Speak briefly."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi."},
            {"role": "user", "content": "Continue"},
        ])
        payload = gateway.request_snapshot(request)

        self.assertEqual(payload["model"], "gemini-3.5-flash-lite")
        self.assertEqual(payload["generation_config"]["thinking_level"], "minimal")
        self.assertNotIn("temperature", payload["generation_config"])
        self.assertEqual(payload["system_instruction"], "Speak briefly.")
        self.assertEqual(
            [step["type"] for step in payload["input"]],
            ["user_input", "model_output", "user_input"],
        )
        system, steps = _interaction_input(request.messages)
        self.assertEqual(system, "Speak briefly.")
        self.assertEqual(steps, payload["input"])
        usage = parse_gemini_usage({
            "total_input_tokens": 75,
            "total_cached_tokens": 50,
            "total_output_tokens": 18,
            "total_thought_tokens": 3,
            "total_tokens": 96,
        })
        self.assertEqual(usage.prompt_tokens, 75)
        self.assertEqual(usage.cached_tokens, 50)
        self.assertEqual(usage.completion_tokens, 18)
        self.assertEqual(usage.reasoning_tokens, 3)
        self.assertEqual(usage.total_tokens, 96)


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "telemetry.sqlite"
        self.recorder = SQLiteTelemetryRecorder(self.db_path)
        await self.recorder.start()

    async def asyncTearDown(self) -> None:
        await self.recorder.close()
        with closing(sqlite3.connect(self.db_path)) as conn:
            for table in ("turns", "llm_calls", "stt_calls", "tts_calls"):
                self.assertEqual(
                    conn.execute(f"SELECT COUNT(*) FROM {table} WHERE status = 'running'").fetchone()[0],
                    0,
                    f"{table} retained running rows after turn completion",
                )
        self.temp_dir.cleanup()

    def orchestrator(self, *, llm=None, stt=None, tts=None, settings=None) -> TurnOrchestrator:
        settings = settings or Settings()
        tts_gateway = tts or FakeTTS()
        return TurnOrchestrator(
            settings=settings,
            llm=llm or FakeLLM(),
            stt=stt or FakeSTT(),
            tts=TTSAdapter([tts_gateway], default_provider=tts_gateway.provider),
            sessions=SessionStore(max_turns=4, max_sessions=10, ttl_seconds=3600),
            telemetry=self.recorder,
        )

    async def test_success_records_separate_capability_tables_and_usage(self) -> None:
        orchestrator = self.orchestrator()
        trace = orchestrator.new_trace(session_id="session-1", kind="voice_realtime")
        events = [
            event
            async for event in orchestrator.stream_realtime_turn(
                trace,
                audio(),
                sample_rate=16000,
                brain_api_key="xai-test-secret",
                voice_api_key="voice-test-secret",
                voice_id="voice-id",
            )
        ]
        await self.recorder.close()
        self.recorder._writer_task = None

        self.assertIn("done", [event.event for event in events])
        self.assertTrue(all(event.data.get("turn_id") == trace.turn_id for event in events))
        timing_event = next(event for event in events if event.event == "transcript_timing")
        self.assertEqual(timing_event.data["words"][0]["start_ms"], 0)
        audio_event = next(event for event in events if event.event == "audio")
        self.assertEqual("".join(audio_event.data["alignment"]["chars"]), "A concise answer.")
        with closing(sqlite3.connect(self.db_path)) as conn:
            turn = conn.execute(
                "SELECT status, user_text, assistant_text, failed_stage, error_id "
                "FROM turns WHERE turn_id = ?",
                (trace.turn_id,),
            ).fetchone()
            llm = conn.execute(
                "SELECT cached_tokens, reasoning_tokens, cache_status, cost_usd_ticks, request_json, "
                "first_token_ms, requested_model, requested_reasoning_setting, response_model, model "
                "FROM llm_calls WHERE turn_id = ?",
                (trace.turn_id,),
            ).fetchone()
            stt = conn.execute(
                "SELECT audio_bytes, audio_duration_ms, partial_count, committed_count FROM stt_calls "
                "WHERE turn_id = ?",
                (trace.turn_id,),
            ).fetchone()
            tts = conn.execute(
                "SELECT input_text, input_chars, audio_bytes, audio_duration_ms FROM tts_calls WHERE turn_id = ?",
                (trace.turn_id,),
            ).fetchone()
            database_text = " ".join(str(row) for row in conn.iterdump())
            event_offsets = dict(
                conn.execute(
                    "SELECT name, offset_ms FROM turn_events WHERE turn_id = ? "
                    "AND name IN ('stt.committed', 'llm.first_token')",
                    (trace.turn_id,),
                ).fetchall()
            )

        self.assertEqual(turn, ("success", None, None, None, None))
        self.assertEqual(llm[:4], (10, 2, "partial", 1234))
        self.assertIsNotNone(llm[5])
        self.assertEqual(llm[6:], ("fake-fast", "low", "fake-returned", "fake-returned"))
        self.assertIn("stt.committed", event_offsets)
        self.assertIn("llm.first_token", event_offsets)
        self.assertGreaterEqual(
            event_offsets["llm.first_token"] - event_offsets["stt.committed"],
            0,
        )
        self.assertGreater(stt[0], 0)
        self.assertAlmostEqual(stt[1], 100.0)
        self.assertEqual(stt[2:], (1, 1))
        self.assertEqual(tts[0], "")
        self.assertEqual(tts[1], len("A concise answer.") + 1)
        self.assertAlmostEqual(tts[3], 100.0)
        self.assertEqual(json.loads(llm[4])["model"], "fake-fast")
        self.assertNotIn("messages", json.loads(llm[4]))
        self.assertNotIn("hello", database_text)
        self.assertNotIn("A concise answer.", database_text)
        self.assertNotIn("xai-test-secret", database_text)
        self.assertNotIn("voice-test-secret", database_text)

    async def test_stt_and_tts_can_use_independent_api_keys(self) -> None:
        stt = FakeSTT()
        tts = FakeTTS()
        orchestrator = self.orchestrator(stt=stt, tts=tts)
        trace = orchestrator.new_trace(session_id="separate-voice-keys", kind="voice_realtime")

        _ = [
            event
            async for event in orchestrator.stream_realtime_turn(
                trace,
                audio(),
                sample_rate=16000,
                brain_api_key="brain-key",
                voice_api_key="legacy-shared-key",
                voice_id="voice-id",
                stt_api_key="stt-only-key",
                tts_api_key="tts-only-key",
            )
        ]

        self.assertEqual(stt.api_keys, ["stt-only-key"])
        self.assertEqual(tts.websocket_api_keys, ["tts-only-key"])

    async def test_tts_failure_marks_partial_failure_and_structured_error(self) -> None:
        orchestrator = self.orchestrator(tts=FailingTTS())
        trace = orchestrator.new_trace(session_id="session-2", kind="voice_realtime")
        events = [
            event
            async for event in orchestrator.stream_realtime_turn(
                trace,
                audio(),
                sample_rate=16000,
                brain_api_key="brain-key",
                voice_api_key="voice-key",
                voice_id="voice-id",
            )
        ]
        await self.recorder.close()
        self.recorder._writer_task = None

        error_event = next(event for event in events if event.event == "tts_error")
        self.assertEqual(error_event.data["stage"], "tts")
        self.assertEqual(error_event.data["code"], "provider_failure")
        self.assertEqual(error_event.data["provider"], "fake_tts")
        self.assertEqual(error_event.data["upstream_status"], 402)
        self.assertEqual(error_event.data["request_id"], "provider-request-id")
        self.assertEqual(error_event.data["provider_detail"]["status"], "quota_exceeded")
        with closing(sqlite3.connect(self.db_path)) as conn:
            turn = conn.execute(
                "SELECT status, failed_stage, error_id FROM turns WHERE turn_id = ?",
                (trace.turn_id,),
            ).fetchone()
            stored = conn.execute(
                "SELECT error_id, code, technical_message, stack_trace FROM errors WHERE turn_id = ?",
                (trace.turn_id,),
            ).fetchone()
            call_states = dict(
                conn.execute(
                    "SELECT 'stt', status FROM stt_calls WHERE turn_id = ? UNION ALL "
                    "SELECT 'llm', status FROM llm_calls WHERE turn_id = ? UNION ALL "
                    "SELECT 'tts', status FROM tts_calls WHERE turn_id = ?",
                    (trace.turn_id, trace.turn_id, trace.turn_id),
                ).fetchall()
            )
        self.assertEqual(turn, ("partial_failure", "tts", stored[0]))
        self.assertEqual(stored[1], "provider_failure")
        self.assertIn("synthetic failure", stored[2])
        self.assertIn("GatewayError", stored[3])
        self.assertEqual(call_states, {"stt": "success", "llm": "success", "tts": "failed"})

    async def test_stt_failure_marks_turn_and_call_failed(self) -> None:
        orchestrator = self.orchestrator(stt=FailingSTT())
        trace = orchestrator.new_trace(session_id="session-stt-failure", kind="voice_realtime")
        events = [
            event
            async for event in orchestrator.stream_realtime_turn(
                trace,
                audio(),
                sample_rate=16000,
                brain_api_key="brain-key",
                voice_api_key="voice-key",
                voice_id="voice-id",
            )
        ]
        await self.recorder.close()
        self.recorder._writer_task = None

        error_event = next(event for event in events if event.event == "error")
        self.assertEqual(error_event.data["stage"], "stt")
        self.assertEqual(error_event.data["code"], "provider_failure")
        with closing(sqlite3.connect(self.db_path)) as conn:
            turn_status, failed_stage, turn_error_id = conn.execute(
                "SELECT status, failed_stage, error_id FROM turns WHERE turn_id = ?",
                (trace.turn_id,),
            ).fetchone()
            call_status = conn.execute(
                "SELECT status, error_id FROM stt_calls WHERE turn_id = ?", (trace.turn_id,)
            ).fetchone()
            llm_count = conn.execute(
                "SELECT COUNT(*) FROM llm_calls WHERE turn_id = ?", (trace.turn_id,)
            ).fetchone()[0]
            tts_count = conn.execute(
                "SELECT COUNT(*) FROM tts_calls WHERE turn_id = ?", (trace.turn_id,)
            ).fetchone()[0]
        self.assertEqual(turn_status, "failed")
        self.assertEqual(failed_stage, "stt")
        self.assertEqual(turn_error_id, call_status[1])
        self.assertEqual(call_status[0], "failed")
        self.assertEqual((llm_count, tts_count), (0, 0))

    async def test_llm_failure_marks_turn_and_call_failed(self) -> None:
        orchestrator = self.orchestrator(llm=FailingLLM())
        trace = orchestrator.new_trace(session_id="session-llm-failure", kind="voice_realtime")
        events = [
            event
            async for event in orchestrator.stream_realtime_turn(
                trace,
                audio(),
                sample_rate=16000,
                brain_api_key="brain-key",
                voice_api_key="voice-key",
                voice_id="voice-id",
            )
        ]
        await self.recorder.close()
        self.recorder._writer_task = None

        error_event = next(event for event in events if event.event == "error")
        self.assertEqual(error_event.data["stage"], "llm")
        self.assertEqual(error_event.data["code"], "provider_failure")
        with closing(sqlite3.connect(self.db_path)) as conn:
            turn_status, failed_stage = conn.execute(
                "SELECT status, failed_stage FROM turns WHERE turn_id = ?", (trace.turn_id,)
            ).fetchone()
            call_status = conn.execute(
                "SELECT status FROM llm_calls WHERE turn_id = ?", (trace.turn_id,)
            ).fetchone()[0]
            stt_status = conn.execute(
                "SELECT status FROM stt_calls WHERE turn_id = ?", (trace.turn_id,)
            ).fetchone()[0]
        self.assertEqual(turn_status, "failed")
        self.assertEqual(failed_stage, "llm")
        self.assertEqual(stt_status, "success")
        self.assertEqual(call_status, "failed")

    async def test_empty_llm_response_preserves_usage_and_fails_turn(self) -> None:
        orchestrator = self.orchestrator(llm=EmptyLLM())
        trace = orchestrator.new_trace(session_id="session-empty-llm", kind="chat_only")
        events = [
            event
            async for event in orchestrator.stream_chat(
                trace,
                user_text="hello",
                with_audio=False,
                brain_api_key="brain-key",
                voice_api_key=None,
                voice_id=None,
            )
        ]
        await self.recorder.close()
        self.recorder._writer_task = None

        self.assertEqual(next(event for event in events if event.event == "error").data["code"], "empty_response")
        with closing(sqlite3.connect(self.db_path)) as conn:
            turn = conn.execute(
                "SELECT status, failed_stage FROM turns WHERE turn_id = ?", (trace.turn_id,)
            ).fetchone()
            llm = conn.execute(
                "SELECT status, reasoning_tokens, cost_usd_ticks, error_id FROM llm_calls "
                "WHERE turn_id = ?",
                (trace.turn_id,),
            ).fetchone()
        self.assertEqual(turn, ("failed", "llm"))
        self.assertEqual(llm[:3], ("failed", 3, 456))
        self.assertTrue(llm[3])

    async def test_empty_tts_audio_is_a_partial_failure(self) -> None:
        orchestrator = self.orchestrator(tts=EmptyTTS())
        trace = orchestrator.new_trace(session_id="session-empty-tts", kind="chat_voice")
        events = [
            event
            async for event in orchestrator.stream_chat(
                trace,
                user_text="hello",
                with_audio=True,
                brain_api_key="brain-key",
                voice_api_key="voice-key",
                voice_id="voice-id",
            )
        ]
        await self.recorder.close()
        self.recorder._writer_task = None

        self.assertEqual(next(event for event in events if event.event == "tts_error").data["code"], "empty_audio")
        with closing(sqlite3.connect(self.db_path)) as conn:
            turn = conn.execute(
                "SELECT status, failed_stage FROM turns WHERE turn_id = ?", (trace.turn_id,)
            ).fetchone()
            calls = dict(
                conn.execute(
                    "SELECT 'llm', status FROM llm_calls WHERE turn_id = ? UNION ALL "
                    "SELECT 'tts', status FROM tts_calls WHERE turn_id = ?",
                    (trace.turn_id, trace.turn_id),
                ).fetchall()
            )
        self.assertEqual(turn, ("partial_failure", "tts"))
        self.assertEqual(calls, {"llm": "success", "tts": "failed"})

    async def test_cancelled_realtime_turn_is_persisted(self) -> None:
        orchestrator = self.orchestrator(stt=SlowSTT())
        trace = orchestrator.new_trace(session_id="session-3", kind="voice_realtime")

        async def consume() -> None:
            async for _ in orchestrator.stream_realtime_turn(
                trace,
                audio(),
                sample_rate=16000,
                brain_api_key="brain-key",
                voice_api_key="voice-key",
                voice_id="voice-id",
            ):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.recorder.close()
        self.recorder._writer_task = None
        with closing(sqlite3.connect(self.db_path)) as conn:
            status = conn.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (trace.turn_id,)
            ).fetchone()[0]
            stt_status = conn.execute(
                "SELECT status FROM stt_calls WHERE turn_id = ?", (trace.turn_id,)
            ).fetchone()[0]
        self.assertEqual(status, "cancelled")
        self.assertEqual(stt_status, "cancelled")


class ContractTests(unittest.TestCase):
    def test_xai_usage_fields_and_cache_status(self) -> None:
        usage = parse_xai_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 30,
                "total_tokens": 130,
                "prompt_tokens_details": {"cached_tokens": 75},
                "completion_tokens_details": {"reasoning_tokens": 12},
                "cost_in_usd_ticks": 9876,
            }
        )
        self.assertEqual(usage.cached_tokens, 75)
        self.assertEqual(usage.reasoning_tokens, 12)
        self.assertEqual(usage.cost_usd_ticks, 9876)
        self.assertEqual(usage.cache_status, "partial")

    def test_chunker_preserves_sentence_boundaries(self) -> None:
        chunks, remaining = pop_ready_speech_chunks("This is a complete sentence. Next")
        self.assertEqual(chunks, ["This is a complete sentence."])
        self.assertEqual(remaining, "Next")

    def test_error_redaction_removes_credentials(self) -> None:
        secret = "xai-" + ("a" * 32)
        self.assertNotIn(secret, redact(f"Authorization: {secret}"))
        bearer = "opaqueCredential" + ("z" * 32)
        self.assertNotIn(bearer, redact(f"Authorization: Bearer {bearer}"))
        self.assertNotIn(bearer, redact(json.dumps({"Authorization": f"Bearer {bearer}"})))
        info = error_info(RuntimeError(f"failed with {secret}"))
        self.assertNotIn(secret, info.technical_message)
        self.assertNotIn(secret, info.stack_trace)

        response = httpx.Response(
            401,
            json={
                "detail": {
                    "type": "authentication_error",
                    "status": "invalid_api_key",
                    "message": f"Rejected Authorization: Bearer {bearer}",
                    "request_id": "provider-request",
                }
            },
        )
        provider_detail = parse_provider_error(response)
        self.assertEqual(provider_detail["status"], "invalid_api_key")
        self.assertNotIn(bearer, provider_detail["message"])

    def test_stt_opening_timeout_has_a_specific_retryable_code(self) -> None:
        error = stt_gateway_error(TimeoutError("opening handshake"), stage="stt", connecting=True)
        self.assertEqual(error.code, "connect_timeout")
        self.assertTrue(error.retryable)

    def test_local_request_and_websocket_origin_boundaries(self) -> None:
        self.assertTrue(is_local_http_request("127.0.0.1", "127.0.0.1:8000"))
        self.assertFalse(is_local_http_request("192.168.1.10", "127.0.0.1:8000"))
        self.assertFalse(is_local_http_request("127.0.0.1", "attacker.invalid"))
        self.assertTrue(
            is_allowed_websocket("127.0.0.1", "127.0.0.1:8000", "http://127.0.0.1:8000")
        )
        self.assertFalse(
            is_allowed_websocket("127.0.0.1", "127.0.0.1:8000", "https://attacker.invalid")
        )

    def test_rate_limiter_caps_client_state(self) -> None:
        limiter = SlidingWindowRateLimiter(requests=1, window_seconds=60, max_clients=100)
        self.assertTrue(all(limiter.consume(f"client-{index}") for index in range(100)))
        self.assertFalse(limiter.consume("client-over-capacity"))
        self.assertLessEqual(len(limiter._timestamps), 100)


class ConnectivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_provider_checks_must_succeed(self) -> None:
        brain = FakeProbe("xai")
        stt = FakeProbe("elevenlabs")
        tts = FakeProbe("alternate_tts", ok=False)
        service = ConnectivityService(brain=brain, stt=stt, tts=tts)

        report = await service.check(
            brain_api_key="brain-key",
            stt_api_key="stt-key",
            tts_api_key="tts-key",
            tts_voice_id="voice-id",
        )

        self.assertFalse(report["ready"])
        self.assertTrue(report["brain"]["ok"])
        self.assertTrue(report["stt"]["ok"])
        self.assertFalse(report["tts"]["ok"])
        self.assertEqual(brain.calls, [("brain-key", None)])
        self.assertEqual(stt.calls, [("stt-key", None)])
        self.assertEqual(tts.calls, [("tts-key", "voice-id")])

    async def test_selected_brain_probe_is_used(self) -> None:
        xai = FakeProbe("xai")
        deepseek = FakeProbe("deepseek")
        stt = FakeProbe("elevenlabs")
        tts = FakeProbe("fake_tts")
        service = ConnectivityService(
            brain=[xai, deepseek],
            default_brain_provider="xai",
            stt=stt,
            tts=tts,
        )

        report = await service.check(
            brain_api_key=None,
            brain_provider="deepseek",
            stt_api_key=None,
            tts_api_key=None,
            tts_voice_id=None,
        )

        self.assertTrue(report["ready"])
        self.assertEqual(report["brain"]["provider"], "deepseek")
        self.assertEqual(xai.calls, [])
        self.assertEqual(deepseek.calls, [(None, None)])

    async def test_connectivity_endpoint_ignores_browser_provider_credentials(self) -> None:
        brain = FakeProbe("xai")
        stt_probe = FakeProbe("elevenlabs")
        tts_probe = FakeProbe("fake_tts")
        settings = Settings(enforce_local_access=False)
        recorder = SQLiteTelemetryRecorder(Path("unused.sqlite"), enabled=False)
        orchestrator = TurnOrchestrator(
            settings=settings,
            llm=FakeLLM(),
            stt=FakeSTT(),
            tts=TTSAdapter([FakeTTS()], default_provider="fake_tts"),
            sessions=SessionStore(max_turns=4, max_sessions=10, ttl_seconds=3600),
            telemetry=recorder,
        )
        services = ApplicationServices(
            settings=settings,
            orchestrator=orchestrator,
            telemetry=recorder,
            rate_limiter=SlidingWindowRateLimiter(requests=20, window_seconds=60),
            connectivity=ConnectivityService(brain=brain, stt=stt_probe, tts=tts_probe),
        )
        app = FastAPI()
        app.include_router(create_router(services))

        with TestClient(app) as client:
            response = client.post(
                "/api/connectivity/check",
                headers={
                    "X-OS1-Brain-API-Key": "browser-brain-key",
                    "X-OS1-Voice-API-Key": "browser-voice-key",
                    "X-OS1-STT-API-Key": "browser-stt-key",
                    "X-OS1-TTS-API-Key": "browser-tts-key",
                    "X-OS1-Voice-Gender": "female",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ready"])
        self.assertEqual(brain.calls, [(None, None)])
        self.assertEqual(
            tts_probe.calls,
            [(None, None)],
        )
        self.assertEqual(stt_probe.calls, [(None, None)])

    async def test_xai_probe_validates_model_and_authentication(self) -> None:
        settings = Settings(
            llm_api_key="server-key",
            llm_base_url="https://api.x.ai/v1",
            llm_model="grok-test",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/models/grok-test")
            if request.headers.get("authorization") != "Bearer valid-key":
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={"id": "grok-test", "object": "model"})

        probe = XAIConnectivityProbe(settings, transport=httpx.MockTransport(handler))
        success = await probe.check(api_key="valid-key")
        rejected = await probe.check(api_key="invalid-key")

        self.assertTrue(success.ok)
        self.assertEqual(success.code, "ok")
        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.code, "authentication_failed")

    async def test_deepseek_probe_requires_configured_model(self) -> None:
        settings = Settings(deepseek_model="deepseek-v4-flash")

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/models")
            self.assertEqual(request.headers.get("authorization"), "Bearer valid-key")
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})

        probe = DeepSeekConnectivityProbe(settings, transport=httpx.MockTransport(handler))
        result = await probe.check(api_key="valid-key")
        self.assertTrue(result.ok)

    async def test_gemini_probe_uses_google_api_key_header(self) -> None:
        settings = Settings(
            gemini_base_url="https://generativelanguage.googleapis.com/v1beta",
            gemini_model="gemini-3.5-flash-lite",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1beta/models/gemini-3.5-flash-lite")
            self.assertEqual(request.headers.get("x-goog-api-key"), "valid-key")
            return httpx.Response(200, json={"name": "models/gemini-3.5-flash-lite"})

        probe = GeminiConnectivityProbe(settings, transport=httpx.MockTransport(handler))
        result = await probe.check(api_key="valid-key")
        self.assertTrue(result.ok)

    async def test_elevenlabs_probe_validates_realtime_capabilities(self) -> None:
        settings = Settings(elevenlabs_api_key="server-key")
        requested_capabilities: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_capabilities.append(request.url.path)
            key = request.headers.get("xi-api-key")
            if key == "exhausted-key":
                return httpx.Response(
                    402,
                    json={
                        "detail": {
                            "type": "payment_required",
                            "status": "quota_exceeded",
                            "message": "Insufficient credits.",
                            "request_id": "quota-request",
                        }
                    },
                )
            if key == "restricted-key":
                return httpx.Response(
                    401,
                    json={
                        "detail": {
                            "type": "authentication_error",
                            "status": "missing_permissions",
                            "message": "Missing speech_to_text permission.",
                        }
                    },
                )
            if key != "valid-key":
                return httpx.Response(401, json={"detail": "unauthorized"})
            return httpx.Response(200, json={"token": "single-use-test-token"})

        probe = ElevenLabsConnectivityProbe(settings, transport=httpx.MockTransport(handler))
        success = await probe.check(api_key="valid-key")
        self.assertTrue(success.ok)
        self.assertEqual(
            requested_capabilities,
            [
                "/v1/single-use-token/realtime_scribe",
                "/v1/single-use-token/tts_websocket",
            ],
        )

        rejected = await probe.check(api_key="invalid-key")
        exhausted = await probe.check(api_key="exhausted-key")
        restricted = await probe.check(api_key="restricted-key")

        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.code, "authentication_failed")
        self.assertFalse(exhausted.ok)
        self.assertEqual(exhausted.code, "payment_required")
        self.assertEqual(exhausted.upstream_status, 402)
        self.assertEqual(exhausted.provider_detail["status"], "quota_exceeded")
        self.assertEqual(exhausted.provider_detail["request_id"], "quota-request")
        self.assertFalse(restricted.ok)
        self.assertEqual(restricted.code, "authorization_failed")
        self.assertEqual(restricted.provider_detail["status"], "missing_permissions")


class TelemetryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_telemetry_secures_existing_default_storage(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX permissions only")
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            data_dir.mkdir(mode=0o755)
            db_path = data_dir / "telemetry.sqlite"
            db_path.touch(mode=0o644)
            wal_path = Path(f"{db_path}-wal")
            shm_path = Path(f"{db_path}-shm")
            wal_path.touch(mode=0o644)
            shm_path.touch(mode=0o644)

            recorder = SQLiteTelemetryRecorder(
                db_path,
                enabled=False,
                manage_parent_permissions=True,
            )
            await recorder.start()

            self.assertIsNone(recorder._writer_task)
            self.assertEqual(data_dir.stat().st_mode & 0o777, 0o700)
            for path in (db_path, wal_path, shm_path):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_custom_telemetry_path_does_not_chmod_existing_parent(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX permissions only")
        with tempfile.TemporaryDirectory() as temp_dir:
            shared_dir = Path(temp_dir) / "shared"
            shared_dir.mkdir(mode=0o755)
            db_path = shared_dir / "telemetry.sqlite"
            db_path.touch(mode=0o644)

            recorder = SQLiteTelemetryRecorder(db_path, enabled=False)
            await recorder.start()

            self.assertEqual(shared_dir.stat().st_mode & 0o777, 0o755)
            self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)

    async def test_schema_flush_retention_and_foreign_key_cascade(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telemetry.sqlite"
            recorder = SQLiteTelemetryRecorder(db_path)
            await recorder.start()
            if os.name == "posix":
                self.assertEqual(db_path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(db_path.stat().st_mode & 0o777, 0o600)
            trace = recorder.new_turn(session_id="persistent-session", kind="chat_only")
            trace.event("llm.requested", stage="llm")
            trace.finish("success", assistant_text="stored", response_complete=True)
            await recorder.close()

            second_recorder = SQLiteTelemetryRecorder(db_path)
            await second_recorder.start()
            await second_recorder.close()

            with closing(sqlite3.connect(db_path)) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(
                    conn.execute("SELECT status FROM turns WHERE turn_id = ?", (trace.turn_id,)).fetchone()[0],
                    "success",
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM turn_events WHERE turn_id = ?", (trace.turn_id,)).fetchone()[0],
                    3,
                )
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("DELETE FROM turns WHERE turn_id = ?", (trace.turn_id,))
                conn.commit()
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM turn_events WHERE turn_id = ?", (trace.turn_id,)).fetchone()[0],
                    0,
                )

    async def test_version_one_database_migrates_without_losing_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telemetry.sqlite"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "CREATE TABLE turns (turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                    "kind TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, "
                    "completed_at TEXT, duration_ms REAL, user_text TEXT, assistant_text TEXT, "
                    "response_complete INTEGER NOT NULL DEFAULT 0)"
                )
                conn.execute(
                    "INSERT INTO turns(turn_id, session_id, kind, status, started_at) "
                    "VALUES ('old-turn', 'old-session', 'chat_only', 'success', '2026-01-01')"
                )
                conn.execute("PRAGMA user_version=1")
                conn.commit()

            recorder = SQLiteTelemetryRecorder(db_path)
            await recorder.start()
            await recorder.close()
            with closing(sqlite3.connect(db_path)) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(turns)")}
                llm_columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_calls)")}
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertIn("failed_stage", columns)
                self.assertIn("error_id", columns)
                self.assertIn("purpose", llm_columns)
                self.assertIn("requested_model", llm_columns)
                self.assertIn("requested_reasoning_setting", llm_columns)
                self.assertIn("response_model", llm_columns)
                self.assertIn("response_reasoning_setting", llm_columns)
                self.assertEqual(
                    conn.execute("SELECT status FROM turns WHERE turn_id='old-turn'").fetchone()[0],
                    "success",
                )

    async def test_bad_telemetry_record_does_not_drop_valid_turn_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telemetry.sqlite"
            recorder = SQLiteTelemetryRecorder(db_path)
            await recorder.start()
            trace = recorder.new_turn(session_id="batch-session", kind="chat_only")
            recorder._enqueue("INSERT INTO table_that_does_not_exist(value) VALUES (?)", ("bad",))
            trace.finish("success", assistant_text="preserved", response_complete=True)
            with self.assertLogs("os1.telemetry", level="ERROR"):
                await recorder.close()
            with closing(sqlite3.connect(db_path)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT status, assistant_text FROM turns WHERE turn_id = ?",
                        (trace.turn_id,),
                    ).fetchone(),
                    ("success", None),
                )

    async def test_usage_summary_aggregates_metrics_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telemetry.sqlite"
            recorder = SQLiteTelemetryRecorder(db_path)
            await recorder.start()
            trace = recorder.new_turn(session_id="usage-session", kind="voice_live")
            trace.update_text(user_text="private user text", assistant_text="private assistant text")
            recorder.start_llm_call(
                trace,
                call_id="usage-llm",
                provider="xai",
                model="grok-test",
                reasoning_effort="low",
                request={"model": "grok-test", "messages": [{"content": "private prompt"}]},
            )
            recorder.finish_llm_call(
                "usage-llm",
                status="success",
                duration_ms=1250,
                first_token_ms=650,
                response_text="private model response",
                prompt_tokens=100,
                cached_tokens=60,
                reasoning_tokens=12,
                completion_tokens=25,
                response_model="grok-returned",
                model="grok-returned",
            )
            recorder.start_llm_call(
                trace,
                call_id="usage-llm-second-sample",
                provider="xai",
                model="grok-test",
                reasoning_effort="low",
                request={"model": "grok-test"},
            )
            recorder.finish_llm_call(
                "usage-llm-second-sample",
                status="success",
                duration_ms=900,
                first_token_ms=1250,
                response_model="grok-returned",
                model="grok-returned",
            )
            recorder.start_llm_call(
                trace,
                call_id="usage-llm-without-ttft",
                provider="xai",
                model="grok-test",
                reasoning_effort="low",
                request={"model": "grok-test"},
            )
            recorder.finish_llm_call(
                "usage-llm-without-ttft",
                status="success",
                duration_ms=700,
                response_model="grok-returned",
                model="grok-returned",
            )
            recorder.start_stt_call(
                trace,
                call_id="usage-stt",
                provider="elevenlabs",
                model="scribe-test",
                sample_rate=16000,
            )
            recorder.finish_stt_call(
                "usage-stt",
                status="success",
                duration_ms=800,
                audio_duration_ms=5000,
                transcript_text="private transcript",
            )
            recorder.start_tts_call(
                trace,
                call_id="usage-tts",
                provider="elevenlabs",
                model="flash-test",
                voice_id="voice-test",
                output_format="pcm_16000",
                sample_rate=16000,
            )
            recorder.finish_tts_call(
                "usage-tts",
                status="failed",
                duration_ms=400,
                input_text="private speech",
                input_chars=42,
                character_cost=40,
                audio_duration_ms=1200,
            )
            trace.finish("partial_failure", assistant_text="private assistant text")
            summary = await recorder.usage_summary("all")
            await recorder.close()

            self.assertFalse(summary["content_recording"])
            self.assertEqual(summary["totals"]["llm"]["cache_hit_tokens"], 60)
            self.assertEqual(summary["totals"]["llm"]["cache_miss_tokens"], 40)
            self.assertEqual(summary["totals"]["llm"]["reasoning_tokens"], 12)
            self.assertEqual(summary["totals"]["llm"]["output_tokens"], 25)
            self.assertEqual(summary["totals"]["llm"]["ttft_sample_count"], 2)
            self.assertEqual(summary["totals"]["llm"]["ttft_p50_ms"], 650.0)
            self.assertEqual(summary["totals"]["llm"]["ttft_p95_ms"], 1250.0)
            self.assertEqual(summary["models"]["llm"][0]["model"], "grok-returned")
            self.assertEqual(summary["models"]["llm"][0]["calls"], 3)
            self.assertEqual(summary["totals"]["elevenlabs"]["calls"], 2)
            self.assertEqual(summary["totals"]["elevenlabs"]["tts_characters"], 40)
            with closing(sqlite3.connect(db_path)) as conn:
                turn = conn.execute(
                    "SELECT user_text, assistant_text FROM turns WHERE turn_id = ?",
                    (trace.turn_id,),
                ).fetchone()
                llm = conn.execute(
                    "SELECT request_json, response_text, requested_model, "
                    "requested_reasoning_setting, response_model, model "
                    "FROM llm_calls WHERE call_id = 'usage-llm'"
                ).fetchone()
                stt_text = conn.execute(
                    "SELECT transcript_text FROM stt_calls WHERE call_id = 'usage-stt'"
                ).fetchone()[0]
                tts_text = conn.execute(
                    "SELECT input_text FROM tts_calls WHERE call_id = 'usage-tts'"
                ).fetchone()[0]
            self.assertEqual(turn, (None, None))
            self.assertNotIn("messages", json.loads(llm[0]))
            self.assertIsNone(llm[1])
            self.assertEqual(llm[2:], ("grok-test", "low", "grok-returned", "grok-returned"))
            self.assertIsNone(stt_text)
            self.assertEqual(tts_text, "")

    async def test_dynamic_telemetry_update_rejects_unknown_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telemetry.sqlite"
            recorder = SQLiteTelemetryRecorder(db_path)
            await recorder.start()
            trace = recorder.new_turn(session_id="column-session", kind="chat_only")
            recorder.start_llm_call(
                trace,
                call_id="column-call",
                provider="fake",
                model="fake",
                reasoning_effort="low",
                request={},
            )
            with self.assertLogs("os1.telemetry", level="ERROR"):
                recorder.finish_llm_call(
                    "column-call",
                    **{"status = 'failed' --": "injected"},
                )
            recorder.finish_llm_call("column-call", status="success")
            trace.finish("success")
            await recorder.close()
            with closing(sqlite3.connect(db_path)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM llm_calls WHERE call_id = 'column-call'"
                    ).fetchone()[0],
                    "success",
                )


class WebSocketIntegrationTests(unittest.TestCase):
    def test_realtime_protocol_records_browser_playback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telemetry.sqlite"
            recorder = SQLiteTelemetryRecorder(db_path)
            settings = Settings(enforce_local_access=False)
            orchestrator = TurnOrchestrator(
                settings=settings,
                llm=FakeLLM(),
                stt=FakeSTT(),
                tts=TTSAdapter([FakeTTS()], default_provider="fake_tts"),
                sessions=SessionStore(max_turns=4, max_sessions=10, ttl_seconds=3600),
                telemetry=recorder,
            )
            services = ApplicationServices(
                settings=settings,
                orchestrator=orchestrator,
                telemetry=recorder,
                rate_limiter=SlidingWindowRateLimiter(requests=20, window_seconds=60),
            )

            @asynccontextmanager
            async def lifespan(_: FastAPI):
                await recorder.start()
                yield
                await recorder.close()

            app = FastAPI(lifespan=lifespan)
            app.include_router(create_realtime_router(services))
            received: list[dict[str, object]] = []
            with TestClient(app) as client:
                with client.websocket_connect("/api/realtime/turn") as websocket:
                    websocket.send_json(
                        {
                            "type": "start",
                            "session_id": "integration-session",
                            "brain_api_key": "integration-brain-secret",
                            "voice_api_key": "integration-voice-secret",
                            "voice_gender": "male",
                            "sample_rate": 16000,
                        }
                    )
                    websocket.send_bytes(b"\x00\x00" * 1600)
                    websocket.send_json({"type": "stop"})
                    websocket.send_json({"type": "stop"})
                    try:
                        while True:
                            payload = websocket.receive_json()
                            received.append(payload)
                            if payload.get("event") == "audio":
                                websocket.send_json(
                                    {
                                        "type": "client_event",
                                        "name": "browser.playback_started",
                                        "turn_id": "wrong-turn",
                                        "client_elapsed_ms": 1.0,
                                    }
                                )
                                for elapsed_ms in (42.5, 43.0):
                                    websocket.send_json(
                                        {
                                            "type": "client_event",
                                            "name": "browser.playback_started",
                                            "turn_id": payload["data"]["turn_id"],
                                            "client_elapsed_ms": elapsed_ms,
                                        }
                                    )
                                websocket.send_json(
                                    {
                                        "type": "client_event",
                                        "name": "browser.playback_failed",
                                        "turn_id": payload["data"]["turn_id"],
                                        "message": "synthetic browser decoder failure",
                                    }
                                )
                    except WebSocketDisconnect:
                        pass

            event_names = [str(payload.get("event")) for payload in received]
            self.assertIn("transcript", event_names)
            self.assertIn("done", event_names)
            self.assertIn("audio", event_names)
            turn_ids = {
                str(payload["data"].get("turn_id"))
                for payload in received
                if isinstance(payload.get("data"), dict) and payload["data"].get("turn_id")
            }
            self.assertEqual(len(turn_ids), 1)
            turn_id = next(iter(turn_ids))
            with closing(sqlite3.connect(db_path)) as conn:
                playback_rows = conn.execute(
                    "SELECT metadata_json FROM turn_events WHERE turn_id = ? AND name = ?",
                    (turn_id, "browser.playback_started"),
                ).fetchall()
                database_text = " ".join(str(row) for row in conn.iterdump())
                stop_count = conn.execute(
                    "SELECT COUNT(*) FROM turn_events WHERE turn_id = ? AND name = ?",
                    (turn_id, "recording.stop_received"),
                ).fetchone()[0]
                turn = conn.execute(
                    "SELECT status, failed_stage FROM turns WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                browser_error = conn.execute(
                    "SELECT code, technical_message FROM errors "
                    "WHERE turn_id = ? AND stage = 'browser'",
                    (turn_id,),
                ).fetchone()
            self.assertEqual(len(playback_rows), 1)
            self.assertEqual(json.loads(playback_rows[0][0])["client_elapsed_ms"], 42.5)
            self.assertEqual(stop_count, 1)
            self.assertEqual(turn, ("partial_failure", "browser"))
            self.assertEqual(browser_error[0], "audio_playback_failed")
            self.assertIn("synthetic browser decoder failure", browser_error[1])
            self.assertNotIn("integration-brain-secret", database_text)
            self.assertNotIn("integration-voice-secret", database_text)

    def test_client_stop_completes_realtime_turn_with_recording_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telemetry.sqlite"
            recorder = SQLiteTelemetryRecorder(db_path)
            settings = Settings(enforce_local_access=False)
            orchestrator = TurnOrchestrator(
                settings=settings,
                llm=FakeLLM(),
                stt=FakeSTT(),
                tts=TTSAdapter([FakeTTS()], default_provider="fake_tts"),
                sessions=SessionStore(max_turns=4, max_sessions=10, ttl_seconds=3600),
                telemetry=recorder,
            )
            services = ApplicationServices(
                settings=settings,
                orchestrator=orchestrator,
                telemetry=recorder,
                rate_limiter=SlidingWindowRateLimiter(requests=20, window_seconds=60),
            )

            @asynccontextmanager
            async def lifespan(_: FastAPI):
                await recorder.start()
                yield
                await recorder.close()

            app = FastAPI(lifespan=lifespan)
            app.include_router(create_realtime_router(services))
            received: list[dict[str, object]] = []
            with TestClient(app) as client:
                with client.websocket_connect("/api/realtime/turn") as websocket:
                    websocket.send_json(
                        {
                            "type": "start",
                            "session_id": "vad-session",
                            "brain_api_key": "brain-key",
                            "voice_api_key": "voice-key",
                            "sample_rate": 16000,
                        }
                    )
                    try:
                        while True:
                            payload = websocket.receive_json()
                            received.append(payload)
                            if payload.get("event") == "stt_ready":
                                websocket.send_bytes(b"\x00\x00" * 160)
                                websocket.send_json({"type": "stop"})
                            if payload.get("event") == "audio":
                                websocket.send_json(
                                    {
                                        "type": "client_event",
                                        "name": "browser.playback_started",
                                        "turn_id": payload["data"]["turn_id"],
                                        "client_elapsed_ms": 150.0,
                                    }
                                )
                    except WebSocketDisconnect:
                        pass

            events = [str(item.get("event")) for item in received]
            self.assertIn("stt_ready", events)
            self.assertIn("recording_stopped", events)
            self.assertIn("transcript", events)
            turn_id = next(
                str(item["data"]["turn_id"])
                for item in received
                if isinstance(item.get("data"), dict) and item["data"].get("turn_id")
            )
            with closing(sqlite3.connect(db_path)) as conn:
                stop_reason = conn.execute(
                    "SELECT metadata_json FROM turn_events WHERE turn_id = ? AND name = ?",
                    (turn_id, "recording.stop_received"),
                ).fetchone()
                status = conn.execute(
                    "SELECT status FROM turns WHERE turn_id = ?", (turn_id,)
                ).fetchone()[0]
            self.assertIsNotNone(stop_reason)
            self.assertEqual(status, "success")

    def test_disconnect_before_stop_cancels_turn_and_stt_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telemetry.sqlite"
            recorder = SQLiteTelemetryRecorder(db_path)
            settings = Settings(enforce_local_access=False)
            orchestrator = TurnOrchestrator(
                settings=settings,
                llm=FakeLLM(),
                stt=FakeSTT(),
                tts=TTSAdapter([FakeTTS()], default_provider="fake_tts"),
                sessions=SessionStore(max_turns=4, max_sessions=10, ttl_seconds=3600),
                telemetry=recorder,
            )
            services = ApplicationServices(
                settings=settings,
                orchestrator=orchestrator,
                telemetry=recorder,
                rate_limiter=SlidingWindowRateLimiter(requests=20, window_seconds=60),
            )

            @asynccontextmanager
            async def lifespan(_: FastAPI):
                await recorder.start()
                yield
                await recorder.close()

            app = FastAPI(lifespan=lifespan)
            app.include_router(create_realtime_router(services))
            turn_id = ""
            with TestClient(app) as client:
                try:
                    with client.websocket_connect("/api/realtime/turn") as websocket:
                        websocket.send_json(
                            {
                                "type": "start",
                                "session_id": "disconnect-session",
                                "brain_api_key": "brain-key",
                                "voice_api_key": "voice-key",
                                "sample_rate": 16000,
                            }
                        )
                        while True:
                            payload = websocket.receive_json()
                            if payload.get("event") == "stt_ready":
                                turn_id = str(payload["data"]["turn_id"])
                                break
                except concurrent.futures.CancelledError:
                    pass

            with closing(sqlite3.connect(db_path)) as conn:
                turn_status, turn_completed = conn.execute(
                    "SELECT status, completed_at FROM turns WHERE turn_id = ?", (turn_id,)
                ).fetchone()
                stt_status, stt_completed = conn.execute(
                    "SELECT status, completed_at FROM stt_calls WHERE turn_id = ?", (turn_id,)
                ).fetchone()
            self.assertEqual(turn_status, "cancelled")
            self.assertEqual(stt_status, "cancelled")
            self.assertLessEqual(stt_completed, turn_completed)

    def test_non_object_realtime_start_is_rejected_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = SQLiteTelemetryRecorder(Path(temp_dir) / "telemetry.sqlite")
            settings = Settings(enforce_local_access=False)
            orchestrator = TurnOrchestrator(
                settings=settings,
                llm=FakeLLM(),
                stt=FakeSTT(),
                tts=TTSAdapter([FakeTTS()], default_provider="fake_tts"),
                sessions=SessionStore(max_turns=4, max_sessions=10, ttl_seconds=3600),
                telemetry=recorder,
            )
            services = ApplicationServices(
                settings=settings,
                orchestrator=orchestrator,
                telemetry=recorder,
                rate_limiter=SlidingWindowRateLimiter(requests=20, window_seconds=60),
            )

            @asynccontextmanager
            async def lifespan(_: FastAPI):
                await recorder.start()
                yield
                await recorder.close()

            app = FastAPI(lifespan=lifespan)
            app.include_router(create_realtime_router(services))
            with TestClient(app) as client:
                with client.websocket_connect("/api/realtime/turn") as websocket:
                    websocket.send_json([])
                    payload = websocket.receive_json()
                    self.assertEqual(payload["event"], "error")
                    self.assertEqual(payload["data"]["message"], "Invalid realtime start message.")
