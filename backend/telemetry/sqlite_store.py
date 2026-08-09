from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..core.errors import ErrorInfo
from ..core.storage import prepare_private_directory, secure_private_files, sqlite_files
from .schema import SCHEMA_SQL, SCHEMA_VERSION


logger = logging.getLogger("os1.telemetry")

_UPDATE_COLUMNS = {
    "llm_calls": {
        "status", "completed_at", "duration_ms", "first_token_ms", "response_text",
        "model", "reasoning_effort", "response_model", "response_reasoning_setting",
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
}

_CONTENT_COLUMNS = {
    "llm_calls": {"response_text"},
    "stt_calls": {"transcript_text"},
    "tts_calls": {"input_text"},
}

_REQUEST_CONTENT_KEYS = {
    "content",
    "contents",
    "input",
    "messages",
    "prompt",
    "text",
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
        # Keep the public tracing API stable while deliberately excluding
        # conversation content from the telemetry database.
        del user_text, assistant_text

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
        del assistant_text
        self.recorder._enqueue(
            "UPDATE turns SET status = ?, completed_at = ?, duration_ms = ?, "
            "response_complete = ? WHERE turn_id = ?",
            (status, utc_now(), duration_ms, int(response_complete), self.turn_id),
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

    async def usage_summary(self, period: str) -> dict[str, object]:
        if period not in {"7d", "30d", "all"}:
            raise ValueError("Usage range must be 7d, 30d, or all.")
        if self._writer_task is not None:
            await self.queue.join()
        if not self.db_path.exists():
            return _empty_usage_summary(period, enabled=self.enabled)
        return await asyncio.to_thread(self._usage_summary_sync, period)

    def new_turn(self, *, session_id: str, kind: str, turn_id: str | None = None) -> TurnTrace:
        return TurnTrace(
            self,
            turn_id=turn_id or uuid.uuid4().hex,
            session_id=session_id,
            kind=kind,
        )

    def record_error(
        self,
        trace: TurnTrace,
        info: ErrorInfo,
        *,
        call_id: str | None = None,
        affect_turn: bool = True,
    ) -> None:
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
        if affect_turn:
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
        reasoning_effort: str | None,
        request: dict[str, object],
        purpose: str = "answer",
    ) -> None:
        self._enqueue(
            "INSERT INTO llm_calls(call_id, turn_id, provider, model, reasoning_effort, "
            "requested_model, requested_reasoning_setting, purpose, status, started_at, request_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call_id, trace.turn_id, provider, model, reasoning_effort,
                model, reasoning_effort, purpose, "running", utc_now(),
                json_text(_without_content(request)),
            ),
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

    def _update(self, table: str, call_id: str, values: dict[str, object]) -> None:
        allowed_columns = _UPDATE_COLUMNS.get(table)
        if not allowed_columns or not values:
            return
        unknown_columns = set(values) - allowed_columns
        if unknown_columns:
            logger.error("Telemetry update rejected unknown columns for %s: %s", table, sorted(unknown_columns))
            return
        values = {
            column: value
            for column, value in values.items()
            if column not in _CONTENT_COLUMNS.get(table, set())
        }
        if not values:
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
            llm_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(llm_calls)").fetchall()
            }
            if "purpose" not in llm_columns:
                conn.execute("ALTER TABLE llm_calls ADD COLUMN purpose TEXT NOT NULL DEFAULT 'answer'")
            for column in (
                "requested_model",
                "requested_reasoning_setting",
                "response_model",
                "response_reasoning_setting",
            ):
                if column not in llm_columns:
                    conn.execute(f"ALTER TABLE llm_calls ADD COLUMN {column} TEXT")  # nosec B608
            conn.execute(
                "UPDATE llm_calls SET requested_model = COALESCE(requested_model, model), "
                "requested_reasoning_setting = COALESCE(requested_reasoning_setting, reasoning_effort)"
            )
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

    def _usage_summary_sync(self, period: str) -> dict[str, object]:
        cutoff = _usage_cutoff(period)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            llm_total = _usage_rows(conn, "llm", cutoff, group="total")[0]
            stt_total = _usage_rows(conn, "stt", cutoff, group="total")[0]
            tts_total = _usage_rows(conn, "tts", cutoff, group="total")[0]
            llm_models = _usage_rows(conn, "llm", cutoff, group="model")
            stt_models = _usage_rows(conn, "stt", cutoff, group="model")
            tts_models = _usage_rows(conn, "tts", cutoff, group="model")
            ttft_samples = _llm_ttft_samples(conn, cutoff)
            day_rows = {
                "llm": _usage_rows(conn, "llm", cutoff, group="day"),
                "stt": _usage_rows(conn, "stt", cutoff, group="day"),
                "tts": _usage_rows(conn, "tts", cutoff, group="day"),
            }

        _attach_ttft(llm_total, llm_models, ttft_samples)

        days: dict[str, dict[str, object]] = {}
        for service, rows in day_rows.items():
            for row in rows:
                day = str(row.pop("day"))
                days.setdefault(day, {"date": day})[service] = row

        elevenlabs_total = _combine_usage(stt_total, tts_total)
        return {
            "range": period,
            "generated_at": utc_now(),
            "telemetry_enabled": self.enabled,
            "content_recording": False,
            "totals": {"llm": llm_total, "elevenlabs": elevenlabs_total},
            "models": {"llm": llm_models, "stt": stt_models, "tts": tts_models},
            "days": [days[day] for day in sorted(days, reverse=True)],
        }


def _without_content(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_content(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _REQUEST_CONTENT_KEYS
        }
    if isinstance(value, list):
        return [_without_content(item) for item in value]
    return value


def _usage_cutoff(period: str) -> str | None:
    days = {"7d": 7, "30d": 30}.get(period)
    if days is None:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="milliseconds")


def _usage_rows(
    conn: sqlite3.Connection,
    service: str,
    cutoff: str | None,
    *,
    group: str,
) -> list[dict[str, object]]:
    definitions = {
        "llm": (
            "llm_calls",
            "SUM(COALESCE(prompt_tokens, 0)) AS input_tokens, "
            "SUM(COALESCE(cached_tokens, 0)) AS cache_hit_tokens, "
            "SUM(MAX(COALESCE(prompt_tokens, 0) - COALESCE(cached_tokens, 0), 0)) AS cache_miss_tokens, "
            "SUM(COALESCE(reasoning_tokens, 0)) AS reasoning_tokens, "
            "SUM(COALESCE(completion_tokens, 0)) AS output_tokens",
        ),
        "stt": (
            "stt_calls",
            "SUM(COALESCE(audio_duration_ms, 0)) AS audio_duration_ms",
        ),
        "tts": (
            "tts_calls",
            "SUM(COALESCE(character_cost, input_chars, 0)) AS characters, "
            "SUM(COALESCE(audio_duration_ms, 0)) AS audio_duration_ms",
        ),
    }
    table, usage_columns = definitions[service]
    llm_model = "COALESCE(response_model, requested_model, model)"
    identity = {
        "total": "",
        "model": f"provider, {llm_model} AS model, " if service == "llm" else "provider, model, ",
        "day": "date(started_at) AS day, ",
    }[group]
    group_by = {
        "total": "",
        "model": (
            f" GROUP BY provider, {llm_model} ORDER BY calls DESC, provider, model"
            if service == "llm"
            else " GROUP BY provider, model ORDER BY calls DESC, provider, model"
        ),
        "day": " GROUP BY date(started_at) ORDER BY day DESC",
    }[group]
    where = " WHERE started_at >= ?" if cutoff else ""
    params: tuple[object, ...] = (cutoff,) if cutoff else ()
    sql = (
        f"SELECT {identity}COUNT(*) AS calls, "  # nosec B608 -- identifiers are fixed above.
        "SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS successful, "
        "SUM(CASE WHEN status IN ('failed', 'partial_failure') THEN 1 ELSE 0 END) AS failed, "
        "SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled, "
        "SUM(COALESCE(duration_ms, 0)) AS duration_ms, "
        "AVG(duration_ms) AS average_duration_ms, "
        f"{usage_columns} FROM {table}{where}{group_by}"  # nosec B608
    )
    rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    if group == "total" and not rows:
        rows = [{}]
    return [_normalize_usage_row(row, service) for row in rows]


def _llm_ttft_samples(
    conn: sqlite3.Connection,
    cutoff: str | None,
) -> list[tuple[str, str, float]]:
    where = " WHERE first_token_ms IS NOT NULL"
    params: tuple[object, ...] = ()
    if cutoff:
        where += " AND started_at >= ?"
        params = (cutoff,)
    return [
        (str(row[0]), str(row[1]), float(row[2]))
        for row in conn.execute(
            "SELECT provider, COALESCE(response_model, requested_model, model), first_token_ms "
            f"FROM llm_calls{where}",  # nosec B608
            params,
        ).fetchall()
    ]


def _attach_ttft(
    total: dict[str, object],
    models: list[dict[str, object]],
    samples: list[tuple[str, str, float]],
) -> None:
    values = [sample[2] for sample in samples]
    total["ttft_sample_count"] = len(values)
    total["ttft_p50_ms"] = _nearest_rank_percentile(values, 0.50)
    total["ttft_p95_ms"] = _nearest_rank_percentile(values, 0.95)
    by_model: dict[tuple[str, str], list[float]] = {}
    for provider, model, value in samples:
        by_model.setdefault((provider, model), []).append(value)
    for row in models:
        model_values = by_model.get((str(row.get("provider") or ""), str(row.get("model") or "")), [])
        row["ttft_sample_count"] = len(model_values)
        row["ttft_p50_ms"] = _nearest_rank_percentile(model_values, 0.50)
        row["ttft_p95_ms"] = _nearest_rank_percentile(model_values, 0.95)


def _nearest_rank_percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 1)


def _normalize_usage_row(row: dict[str, object], service: str) -> dict[str, object]:
    integer_fields = {"calls", "successful", "failed", "cancelled"}
    if service == "llm":
        integer_fields.update(
            {"input_tokens", "cache_hit_tokens", "cache_miss_tokens", "reasoning_tokens", "output_tokens"}
        )
    elif service == "tts":
        integer_fields.add("characters")
    float_fields = {"duration_ms", "average_duration_ms"}
    if service in {"stt", "tts"}:
        float_fields.add("audio_duration_ms")
    for field in integer_fields:
        row[field] = int(row.get(field) or 0)
    for field in float_fields:
        row[field] = round(float(row.get(field) or 0), 1)
    return row


def _combine_usage(stt: dict[str, object], tts: dict[str, object]) -> dict[str, object]:
    return {
        "calls": int(stt["calls"]) + int(tts["calls"]),
        "successful": int(stt["successful"]) + int(tts["successful"]),
        "failed": int(stt["failed"]) + int(tts["failed"]),
        "cancelled": int(stt["cancelled"]) + int(tts["cancelled"]),
        "duration_ms": round(float(stt["duration_ms"]) + float(tts["duration_ms"]), 1),
        "stt_audio_duration_ms": float(stt["audio_duration_ms"]),
        "tts_audio_duration_ms": float(tts["audio_duration_ms"]),
        "tts_characters": int(tts["characters"]),
    }


def _empty_usage_summary(period: str, *, enabled: bool) -> dict[str, object]:
    llm = _normalize_usage_row({}, "llm")
    llm["ttft_sample_count"] = 0
    llm["ttft_p50_ms"] = None
    llm["ttft_p95_ms"] = None
    stt = _normalize_usage_row({}, "stt")
    tts = _normalize_usage_row({}, "tts")
    return {
        "range": period,
        "generated_at": utc_now(),
        "telemetry_enabled": enabled,
        "content_recording": False,
        "totals": {"llm": llm, "elevenlabs": _combine_usage(stt, tts)},
        "models": {"llm": [], "stt": [], "tts": []},
        "days": [],
    }
