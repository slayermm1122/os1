from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


INTERRUPTED = "(interrupted)"
INTERRUPTED_BY_USER = "(interrupted by user)"


class JSONLChatHistory:
    """Append-only, human-readable conversation history, separate from model context."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def append_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        interrupted: bool = False,
        playback_started: bool = False,
        spoken_text: str = "",
    ) -> None:
        user_record = user_text.strip()
        assistant_record = assistant_text.strip()
        if interrupted and playback_started:
            prefix = _safe_prefix(assistant_record, spoken_text)
            assistant_record = (
                f"{prefix}{INTERRUPTED_BY_USER}{assistant_record[len(prefix):]}"
            )
        elif interrupted:
            user_record = f"{user_record}{INTERRUPTED}"
            assistant_record = f"{INTERRUPTED}{assistant_record}"
        record = {
            "session_id": session_id,
            "turn_id": turn_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "user": user_record,
            "assistant": assistant_record,
            "interrupted": interrupted,
            "playback_started": playback_started,
            "phase": "complete",
        }
        self._append(record)

    def append_user(self, *, session_id: str, turn_id: str, user_text: str) -> None:
        self._append({
            "session_id": session_id,
            "turn_id": turn_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "user": user_text.strip(),
            "assistant": "",
            "interrupted": False,
            "playback_started": False,
            "phase": "user",
        })

    def _append(self, record: dict[str, object]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def session(self, session_id: str) -> list[dict[str, object]]:
        selected = str(session_id or "").strip()
        if not selected or not self.path.exists():
            return []
        records: dict[str, dict[str, object]] = {}
        order: list[str] = []
        with self._lock:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(record, dict) and record.get("session_id") == selected:
                        turn_id = str(record.get("turn_id") or "")
                        if not turn_id:
                            continue
                        if turn_id not in records:
                            order.append(turn_id)
                        records[turn_id] = record
        return [records[turn_id] for turn_id in order]


def _safe_prefix(generated: str, spoken: str) -> str:
    candidate = str(spoken or "").strip()
    if generated.startswith(candidate):
        return candidate
    length = 0
    for expected, actual in zip(generated, candidate):
        if expected != actual:
            break
        length += 1
    return generated[:length].rstrip()
