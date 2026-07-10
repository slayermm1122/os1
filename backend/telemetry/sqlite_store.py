from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.errors import ErrorInfo
from ..core.storage import prepare_private_directory, secure_private_files, sqlite_files
from .schema import SCHEMA_SQL, SCHEMA_VERSION


logger = logging.getLogger("os1.telemetry")

_UPDATE_COLUMNS = {
    "llm_calls": {
        "status", "completed_at", "duration_ms", "first_token_ms", "response_text",
        "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
        "reasoning_tokens", "cache_status", "cost_usd_ticks", "finish_reason",
        "provider_request_id", "system_fingerprint", "service_tier", "error_id",
    },
    "stt_calls": {
        "status", "completed_at", "duration_ms", "audio_bytes", "audio_duration_ms",
        "partial_count", "committed_count", "first_partial_ms", "commit_latency_ms",
        "transcript_text", "language_code", "provider_request_id", "error_id",
    },
    "tts_calls": {
        "status", "completed_at", "duration_ms", "input_text", "input_chars",
        "input_chunks", "first_text_ms", "first_audio_ms", "audio_bytes",
        "audio_duration_ms", "character_cost", "provider_request_id", "trace_id", "error_id",
    },
    "knowledge_calls": {
        "status", "completed_at", "duration_ms", "outcome", "hit_count",
        "results_json", "error_id",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class _Write:
    sql: str
    params: tuple[object, ...]


class TurnTrace:
    def __init__(
        self,
        recorder: "SQLiteTelemetryRecorder",
        *,
        turn_id: str,
        session_id: str,
        kind: str,
    ) -> None:
        self.recorder = recorder
        self.turn_id = turn_id
        self.session_id = session_id
        self.kind = kind
        self.started_at = utc_now()
        self.started_ns = time.perf_counter_ns()
        self.finished = False
        self._force_partial_failure = False
        self._terminal_override: str | None = None
        recorder._enqueue(
            "INSERT INTO turns(turn_id, session_id, kind, status, started_at) VALUES (?, ?, ?, ?, ?)",
            (turn_id, session_id, kind, "running", self.started_at),
        )
        self.event("turn.started", stage="turn")

    def offset_ms(self) -> float:
        return (time.perf_counter_ns() - self.started_ns) / 1_000_000

    def event(
        self,
        name: str,
        *,
        stage: str | None = None,
        metadata: dict[str, object] | None = None,
        offset_ms: float | None = None,
    ) -> None:
        self.recorder._enqueue(
            "INSERT INTO turn_events(turn_id, name, stage, occurred_at, offset_ms, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.turn_id,
                name,
                stage,
                utc_now(),
                self.offset_ms() if offset_ms is None else offset_ms,
                json_text(metadata or {}),
            ),
        )

    def update_text(self, *, user_text: str | None = None, assistant_text: str | None = None) -> None:
        if user_text is not None:
            self.recorder._enqueue(
                "UPDATE turns SET user_text = ? WHERE turn_id = ?",
                (user_text, self.turn_id),
            )
        if assistant_text is not None:
            self.recorder._enqueue(
                "UPDATE turns SET assistant_text = ? WHERE turn_id = ?",
                (assistant_text, self.turn_id),
            )

    def finish(
        self,
        status: str,
        *,
        assistant_text: str | None = None,
        response_complete: bool = False,
    ) -> None:
        if self.finished:
            return
        self.finished = True
        if self._terminal_override:
            status = self._terminal_override
        elif self._force_partial_failure and status == "success":
            status = "partial_failure"
        duration_ms = self.offset_ms()
        self.event("turn.completed", stage="turn", metadata={"status": status}, offset_ms=duration_ms)
        self.recorder._enqueue(
            "UPDATE turns SET status = ?, completed_at = ?, duration_ms = ?, "
            "assistant_text = COALESCE(?, assistant_text), response_complete = ? WHERE turn_id = ?",
            (
                status,
                utc_now(),
                duration_ms,
                assistant_text,
                int(response_complete),
                self.turn_id,
            ),
        )

    def mark_partial_failure(self) -> None:
        self._force_partial_failure = True
        if self.finished:
            self.recorder._enqueue(
                "UPDATE turns SET status = 'partial_failure' "
                "WHERE turn_id = ? AND status = 'success'",
                (self.turn_id,),
            )

    def require_terminal_status(self, status: str) -> None:
        self._terminal_override = status


class SQLiteTelemetryRecorder:
    def __init__(
        self,
        db_path: Path,
        *,
        enabled: bool = True,
        queue_size: int = 2048,
        manage_parent_permissions: bool = False,
    ) -> None:
        self.db_path = db_path
        self.enabled = enabled
        self.manage_parent_permissions = manage_parent_permissions
        self.queue: asyncio.Queue[_Write | None] = asyncio.Queue(maxsize=max(queue_size, 32))
        self._writer_task: asyncio.Task[None] | None = None
        self._accepting = enabled

    async def start(self) -> None:
        if self._writer_task is not None:
            return
        try:
            await asyncio.to_thread(self._secure_existing_storage)
            if not self.enabled:
                return
            await asyncio.to_thread(self._initialize)
        except Exception:
            self._accepting = False
            logger.exception("Telemetry initialization failed; continuing without persistence")
            return
        self._writer_task = asyncio.create_task(self._writer(), name="os1-telemetry-writer")

    async def close(self) -> None:
        if self._writer_task is None:
            return
        self._accepting = False
        await self.queue.put(None)
        await self._writer_task
        self._writer_task = None

    def new_turn(self, *, session_id: str, kind: str, turn_id: str | None = None) -> TurnTrace:
        return TurnTrace(
            self,
            turn_id=turn_id or uuid.uuid4().hex,
            session_id=session_id,
            kind=kind,
        )

    def record_error(self, trace: TurnTrace, info: ErrorInfo, *, call_id: str | None = None) -> None:
        trace.event(
            f"{info.stage}.error",
            stage=info.stage,
            metadata={"error_id": info.error_id, "code": info.code},
        )
        self._enqueue(
            "INSERT INTO errors(error_id, turn_id, call_id, stage, provider, code, retryable, "
            "upstream_status, provider_request_id, public_message, technical_message, exception_type, "
            "stack_trace, occurred_at, offset_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                info.error_id,
                trace.turn_id,
                call_id,
                info.stage,
                info.provider,
                info.code,
                int(info.retryable),
                info.upstream_status,
                info.request_id,
                info.public_message,
                info.technical_message,
                info.exception_type,
                info.stack_trace,
                utc_now(),
                trace.offset_ms(),
            ),
        )
        self._enqueue(
            "UPDATE turns SET failed_stage = COALESCE(failed_stage, ?), "
            "error_id = COALESCE(error_id, ?) WHERE turn_id = ?",
            (info.stage, info.error_id, trace.turn_id),
        )

    def start_llm_call(
        self,
        trace: TurnTrace,
        *,
        call_id: str,
        provider: str,
        model: str,
        reasoning_effort: str,
        request: dict[str, object],
    ) -> None:
        self._enqueue(
            "INSERT INTO llm_calls(call_id, turn_id, provider, model, reasoning_effort, status, "
            "started_at, request_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (call_id, trace.turn_id, provider, model, reasoning_effort, "running", utc_now(), json_text(request)),
        )

    def finish_llm_call(self, call_id: str, **values: object) -> None:
        self._update("llm_calls", call_id, values)

    def start_stt_call(
        self,
        trace: TurnTrace,
        *,
        call_id: str,
        provider: str,
        model: str,
        sample_rate: int | None,
    ) -> None:
        self._enqueue(
            "INSERT INTO stt_calls(call_id, turn_id, provider, model, status, started_at, sample_rate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (call_id, trace.turn_id, provider, model, "running", utc_now(), sample_rate),
        )

    def finish_stt_call(self, call_id: str, **values: object) -> None:
        self._update("stt_calls", call_id, values)

    def start_tts_call(
        self,
        trace: TurnTrace,
        *,
        call_id: str,
        provider: str,
        model: str,
        voice_id: str | None,
        output_format: str,
        sample_rate: int | None,
    ) -> None:
        self._enqueue(
            "INSERT INTO tts_calls(call_id, turn_id, provider, model, voice_id, status, started_at, "
            "output_format, sample_rate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call_id,
                trace.turn_id,
                provider,
                model,
                voice_id,
                "running",
                utc_now(),
                output_format,
                sample_rate,
            ),
        )

    def finish_tts_call(self, call_id: str, **values: object) -> None:
        self._update("tts_calls", call_id, values)

    def start_knowledge_call(
        self,
        trace: TurnTrace,
        *,
        call_id: str,
        provider: str,
        enabled: bool,
        query_text: str,
    ) -> None:
        self._enqueue(
            "INSERT INTO knowledge_calls(call_id, turn_id, provider, enabled, status, started_at, "
            "query_text, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call_id,
                trace.turn_id,
                provider,
                int(enabled),
                "running",
                utc_now(),
                query_text,
                "pending",
            ),
        )

    def finish_knowledge_call(self, call_id: str, **values: object) -> None:
        if "results_json" in values and not isinstance(values["results_json"], str):
            values["results_json"] = json_text(values["results_json"])
        self._update("knowledge_calls", call_id, values)

    def _update(self, table: str, call_id: str, values: dict[str, object]) -> None:
        allowed_columns = _UPDATE_COLUMNS.get(table)
        if not allowed_columns or not values:
            return
        unknown_columns = set(values) - allowed_columns
        if unknown_columns:
            logger.error("Telemetry update rejected unknown columns for %s: %s", table, sorted(unknown_columns))
            return
        columns = ", ".join(f"{column} = ?" for column in values)
        # SQL identifiers are restricted by the per-table allowlist above.
        self._enqueue(
            f"UPDATE {table} SET {columns} WHERE call_id = ?",  # nosec B608
            (*values.values(), call_id),
        )

    def _enqueue(self, sql: str, params: tuple[object, ...]) -> None:
        if not self._accepting:
            return
        try:
            self.queue.put_nowait(_Write(sql, params))
        except asyncio.QueueFull:
            logger.warning("Telemetry queue is full; dropping record")

    async def _writer(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                self.queue.task_done()
                break
            batch = [item]
            while len(batch) < 100:
                try:
                    next_item = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if next_item is None:
                    self.queue.task_done()
                    await self._write_batch(batch)
                    for _ in batch:
                        self.queue.task_done()
                    return
                batch.append(next_item)
            await self._write_batch(batch)
            for _ in batch:
                self.queue.task_done()

    async def _write_batch(self, batch: list[_Write]) -> None:
        try:
            await asyncio.to_thread(self._write_batch_sync, batch)
        except Exception:
            logger.exception("Telemetry batch failed; retrying records individually")
            await asyncio.to_thread(self._write_individually_sync, batch)

    def _initialize(self) -> None:
        self._secure_storage_paths()
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Telemetry schema {current_version} is newer than supported {SCHEMA_VERSION}."
                )
            conn.executescript(SCHEMA_SQL)
            turn_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(turns)").fetchall()
            }
            if "failed_stage" not in turn_columns:
                conn.execute("ALTER TABLE turns ADD COLUMN failed_stage TEXT")
            if "error_id" not in turn_columns:
                conn.execute("ALTER TABLE turns ADD COLUMN error_id TEXT")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
            self._secure_storage_paths()

    def _write_batch_sync(self, batch: list[_Write]) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            for command in batch:
                conn.execute(command.sql, command.params)
            conn.commit()
            self._secure_storage_paths()

    def _write_individually_sync(self, batch: list[_Write]) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            for command in batch:
                try:
                    conn.execute(command.sql, command.params)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    logger.exception("Telemetry record failed and was dropped")
            self._secure_storage_paths()

    def _secure_storage_paths(self) -> None:
        prepare_private_directory(
            self.db_path.parent,
            manage_existing=self.manage_parent_permissions,
        )
        secure_private_files(sqlite_files(self.db_path))

    def _secure_existing_storage(self) -> None:
        paths = sqlite_files(self.db_path)
        if not any(path.exists() for path in paths):
            return
        if self.manage_parent_permissions and self.db_path.parent.exists():
            prepare_private_directory(self.db_path.parent, manage_existing=True)
        secure_private_files(paths)
