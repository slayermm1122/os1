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

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.realtime import create_realtime_router
from backend.config import Settings
from backend.core.chunking import pop_ready_speech_chunks
from backend.core.errors import GatewayError, error_info, redact
from backend.core.orchestrator import TurnOrchestrator
from backend.core.rate_limit import SlidingWindowRateLimiter
from backend.core.security import is_allowed_websocket, is_local_http_request
from backend.core.sessions import SessionStore
from backend.gateways.knowledge import SQLiteFTSKnowledgeGateway, SearchHit
from backend.gateways.llm import LLMRequest, LLMStreamEvent, LLMUsage
from backend.gateways.llm.xai import _parse_usage
from backend.gateways.stt import STTEvent, STTResult
from backend.gateways.stt.elevenlabs import _gateway_error as stt_gateway_error
from backend.gateways.tts import TTSEvent
from backend.telemetry import SQLiteTelemetryRecorder
from backend.services import ApplicationServices


class FakeKnowledge:
    provider = "fake_knowledge"
    enabled = False

    def ensure_index(self) -> None:
        return None

    def reindex(self) -> int:
        return 0

    def search(self, query: str, limit: int | None = None) -> list[SearchHit]:
        return []

    def format_hits(self, hits: list[SearchHit]) -> str:
        return ""


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


class FakeSTT:
    provider = "fake_stt"
    upload_model = "fake-upload"
    realtime_model = "fake-realtime"

    async def transcribe_upload(self, **kwargs) -> STTResult:
        return STTResult(text="hello", raw={"text": "hello"}, request_id="stt-upload")

    async def stream_realtime(
        self,
        audio_chunks: AsyncIterable[bytes],
        *,
        sample_rate: int,
        api_key: str | None = None,
    ) -> AsyncIterator[STTEvent]:
        yield STTEvent(kind="session_started", request_id="stt-session")
        async for _ in audio_chunks:
            pass
        yield STTEvent(kind="partial", text="hel", request_id="stt-session")
        yield STTEvent(kind="committed", text="hello", language_code="en", request_id="stt-session")


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

    async def stream_http(self, text: str, **kwargs) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(kind="audio", audio=b"\x00\x00" * 160)
        yield TTSEvent(kind="complete", request_id="tts-http", character_cost=len(text))

    async def stream_websocket(
        self,
        text_chunks: AsyncIterable[str],
        **kwargs,
    ) -> AsyncIterator[TTSEvent]:
        async for _ in text_chunks:
            pass
        yield TTSEvent(kind="audio", audio=b"\x00\x00" * 1600)
        yield TTSEvent(kind="complete", request_id="tts-ws")


class FailingTTS(FakeTTS):
    async def stream_websocket(self, *args, **kwargs) -> AsyncIterator[TTSEvent]:
        raise GatewayError(
            stage="tts",
            provider=self.provider,
            code="provider_failure",
            public_message="Voice failed.",
            technical_message="synthetic failure",
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


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "telemetry.sqlite"
        self.recorder = SQLiteTelemetryRecorder(self.db_path)
        await self.recorder.start()

    async def asyncTearDown(self) -> None:
        await self.recorder.close()
        with closing(sqlite3.connect(self.db_path)) as conn:
            for table in ("turns", "llm_calls", "stt_calls", "tts_calls", "knowledge_calls"):
                self.assertEqual(
                    conn.execute(f"SELECT COUNT(*) FROM {table} WHERE status = 'running'").fetchone()[0],
                    0,
                    f"{table} retained running rows after turn completion",
                )
        self.temp_dir.cleanup()

    def orchestrator(self, *, llm=None, stt=None, tts=None, settings=None) -> TurnOrchestrator:
        settings = settings or Settings()
        return TurnOrchestrator(
            settings=settings,
            llm=llm or FakeLLM(),
            stt=stt or FakeSTT(),
            tts=tts or FakeTTS(),
            knowledge=FakeKnowledge(),
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
        with closing(sqlite3.connect(self.db_path)) as conn:
            turn = conn.execute(
                "SELECT status, user_text, assistant_text, failed_stage, error_id "
                "FROM turns WHERE turn_id = ?",
                (trace.turn_id,),
            ).fetchone()
            llm = conn.execute(
                "SELECT cached_tokens, reasoning_tokens, cache_status, cost_usd_ticks, request_json "
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

        self.assertEqual(turn, ("success", "hello", "A concise answer.", None, None))
        self.assertEqual(llm[:4], (10, 2, "partial", 1234))
        self.assertIn("A concise answer", tts and turn[2])
        self.assertGreater(stt[0], 0)
        self.assertAlmostEqual(stt[1], 100.0)
        self.assertEqual(stt[2:], (1, 1))
        self.assertEqual(tts[0], "A concise answer.")
        self.assertEqual(tts[1], len(tts[0]) + 1)
        self.assertAlmostEqual(tts[3], 100.0)
        self.assertIn("system", llm[4])
        self.assertNotIn("xai-test-secret", database_text)
        self.assertNotIn("voice-test-secret", database_text)

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
        usage = _parse_usage(
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
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
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
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertIn("failed_stage", columns)
                self.assertIn("error_id", columns)
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
                    ("success", "preserved"),
                )

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


class KnowledgeStorageTests(unittest.TestCase):
    def test_default_knowledge_storage_is_private(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX permissions only")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = Settings(
                root_dir=root,
                frontend_dir=root / "frontend",
                knowledge_db_path=root / "data" / "knowledge.sqlite",
                knowledge_docs_dir=root / "knowledge_docs",
            )
            gateway = SQLiteFTSKnowledgeGateway(settings)
            gateway.ensure_index()

            self.assertEqual((root / "data").stat().st_mode & 0o777, 0o700)
            self.assertEqual(settings.knowledge_docs_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(settings.knowledge_db_path.stat().st_mode & 0o777, 0o600)


class WebSocketIntegrationTests(unittest.TestCase):
    def test_realtime_protocol_records_browser_playback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telemetry.sqlite"
            recorder = SQLiteTelemetryRecorder(db_path)
            settings = Settings(enforce_local_access=False)
            knowledge = FakeKnowledge()
            orchestrator = TurnOrchestrator(
                settings=settings,
                llm=FakeLLM(),
                stt=FakeSTT(),
                tts=FakeTTS(),
                knowledge=knowledge,
                sessions=SessionStore(max_turns=4, max_sessions=10, ttl_seconds=3600),
                telemetry=recorder,
            )
            services = ApplicationServices(
                settings=settings,
                orchestrator=orchestrator,
                knowledge=knowledge,
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

    def test_server_recording_limit_commits_when_client_never_sends_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telemetry.sqlite"
            recorder = SQLiteTelemetryRecorder(db_path)
            settings = Settings(max_recording_seconds=0.05, enforce_local_access=False)
            knowledge = FakeKnowledge()
            orchestrator = TurnOrchestrator(
                settings=settings,
                llm=FakeLLM(),
                stt=FakeSTT(),
                tts=FakeTTS(),
                knowledge=knowledge,
                sessions=SessionStore(max_turns=4, max_sessions=10, ttl_seconds=3600),
                telemetry=recorder,
            )
            services = ApplicationServices(
                settings=settings,
                orchestrator=orchestrator,
                knowledge=knowledge,
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
                            "session_id": "limit-session",
                            "brain_api_key": "brain-key",
                            "voice_api_key": "voice-key",
                            "sample_rate": 16000,
                        }
                    )
                    try:
                        while True:
                            payload = websocket.receive_json()
                            received.append(payload)
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

            self.assertIn("stt_ready", [str(item.get("event")) for item in received])
            self.assertIn("recording_stopped", [str(item.get("event")) for item in received])
            turn_id = next(
                str(item["data"]["turn_id"])
                for item in received
                if isinstance(item.get("data"), dict) and item["data"].get("turn_id")
            )
            with closing(sqlite3.connect(db_path)) as conn:
                limit_count = conn.execute(
                    "SELECT COUNT(*) FROM turn_events WHERE turn_id = ? AND name = ?",
                    (turn_id, "recording.limit_reached"),
                ).fetchone()[0]
                status = conn.execute(
                    "SELECT status FROM turns WHERE turn_id = ?", (turn_id,)
                ).fetchone()[0]
            self.assertEqual(limit_count, 1)
            self.assertEqual(status, "success")

    def test_disconnect_before_stop_cancels_turn_and_stt_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telemetry.sqlite"
            recorder = SQLiteTelemetryRecorder(db_path)
            settings = Settings(max_recording_seconds=15, enforce_local_access=False)
            knowledge = FakeKnowledge()
            orchestrator = TurnOrchestrator(
                settings=settings,
                llm=FakeLLM(),
                stt=FakeSTT(),
                tts=FakeTTS(),
                knowledge=knowledge,
                sessions=SessionStore(max_turns=4, max_sessions=10, ttl_seconds=3600),
                telemetry=recorder,
            )
            services = ApplicationServices(
                settings=settings,
                orchestrator=orchestrator,
                knowledge=knowledge,
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
            knowledge = FakeKnowledge()
            orchestrator = TurnOrchestrator(
                settings=settings,
                llm=FakeLLM(),
                stt=FakeSTT(),
                tts=FakeTTS(),
                knowledge=knowledge,
                sessions=SessionStore(max_turns=4, max_sessions=10, ttl_seconds=3600),
                telemetry=recorder,
            )
            services = ApplicationServices(
                settings=settings,
                orchestrator=orchestrator,
                knowledge=knowledge,
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
