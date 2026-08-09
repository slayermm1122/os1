from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from dotenv import set_key

from ..config import Settings
from .messages import normalize_persona, sanitize_person_name


_VOICE_ID_RE = re.compile(r"[A-Za-z0-9_-]{5,128}$")


class LocalSettingsService:
    """The single writer for non-secret UI settings persisted in local .env."""

    def __init__(self, settings: Settings, env_path: Path) -> None:
        self.settings = settings
        self.env_path = env_path
        self._lock = threading.RLock()

    def select_voice(self, voice_id: str, language: str) -> None:
        selected = voice_id.strip()
        if not _VOICE_ID_RE.fullmatch(selected):
            raise ValueError("Invalid ElevenLabs voice id.")
        selected_language = "zh" if str(language or "").lower().startswith("zh") else "en"
        with self._lock:
            self._set("ELEVENLABS_VOICE_ID", selected)
            self._set("ELEVENLABS_VOICE_LANGUAGE", selected_language)
            self.settings.elevenlabs_voice_id = selected
            self.settings.elevenlabs_voice_language = selected_language

    def update_voice_profile(
        self,
        *,
        vad_silence_threshold_secs: float,
        vad_threshold: float,
    ) -> None:
        silence = min(3.0, max(0.3, float(vad_silence_threshold_secs)))
        threshold = min(0.9, max(0.1, float(vad_threshold)))
        with self._lock:
            self._set("ELEVENLABS_STT_VAD_SILENCE_THRESHOLD_SECS", _number(silence))
            self._set("ELEVENLABS_STT_VAD_THRESHOLD", _number(threshold))
            self.settings.elevenlabs_stt_vad_silence_threshold_secs = silence
            self.settings.elevenlabs_stt_vad_threshold = threshold

    def update_persona(
        self,
        *,
        assistant_name: str,
        user_name: str,
        persona: str,
    ) -> None:
        clean_assistant_name = sanitize_person_name(assistant_name)
        clean_user_name = sanitize_person_name(user_name)
        clean_persona = normalize_persona(persona)
        if assistant_name.strip() and not clean_assistant_name:
            raise ValueError("Name of AI may contain letters, spaces, hyphens, or apostrophes.")
        if user_name.strip() and not clean_user_name:
            raise ValueError("Name of user may contain letters, spaces, hyphens, or apostrophes.")
        if persona.strip().lower() not in {"default", "concise", "conversational"}:
            raise ValueError("Persona must be default, concise, or conversational.")
        with self._lock:
            self._set("ASSISTANT_NAME", clean_assistant_name)
            self._set("USER_NAME", clean_user_name)
            self._set("ASSISTANT_PERSONA", clean_persona)
            self.settings.assistant_name = clean_assistant_name
            self.settings.user_name = clean_user_name
            self.settings.assistant_persona = clean_persona

    def _set(self, key: str, value: str) -> None:
        self.env_path.touch(mode=0o600, exist_ok=True)
        set_key(str(self.env_path), key, value, quote_mode="never")
        if os.name == "posix":
            self.env_path.chmod(0o600)


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")
