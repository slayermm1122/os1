from __future__ import annotations

import json
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
        del language
        with self._lock:
            self._set("ELEVENLABS_VOICE_ID", selected)
            self.settings.elevenlabs_voice_id = selected

    def update_response_language(self, mode: str) -> None:
        selected = str(mode or "").strip().lower()
        if selected not in {"auto", "en", "zh"}:
            raise ValueError("Response language must be auto, en, or zh.")
        with self._lock:
            self._set("ASSISTANT_RESPONSE_LANGUAGE", selected)
            self.settings.assistant_response_language = selected

    def update_brain_provider(self, provider: str) -> str:
        selected = str(provider or "").strip().lower()
        if selected not in {"xai", "deepseek", "google"}:
            raise ValueError("Brain provider must be xai, deepseek, or google.")
        with self._lock:
            self._set("LLM_PROVIDER", selected)
            self.settings.llm_provider = selected
        return selected

    def update_stt_keyterms(self, keyterms: list[str]) -> tuple[str, ...]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in keyterms:
            term = " ".join(str(raw or "").split()).strip()
            if not term:
                continue
            if len(term) > 20:
                raise ValueError("Realtime Scribe keyterms may contain at most 20 characters.")
            if any(character in term for character in "<>{}[]\\"):
                raise ValueError("A keyterm contains unsupported punctuation.")
            folded = term.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            cleaned.append(term)
        if len(cleaned) > 50:
            raise ValueError("Realtime Scribe supports at most 50 keyterms.")
        result = tuple(cleaned)
        with self._lock:
            self._set_json("ELEVENLABS_STT_KEYTERMS_JSON", list(result))
            self.settings.elevenlabs_stt_keyterms = result
        return result

    def update_pronunciation_locator(self, dictionary_id: str, version_id: str) -> None:
        selected_dictionary = str(dictionary_id or "").strip()
        selected_version = str(version_id or "").strip()
        if bool(selected_dictionary) != bool(selected_version):
            raise ValueError("Pronunciation dictionary id and version must be set together.")
        with self._lock:
            self._set("ELEVENLABS_PRONUNCIATION_DICTIONARY_ID", selected_dictionary)
            self._set("ELEVENLABS_PRONUNCIATION_DICTIONARY_VERSION_ID", selected_version)
            self.settings.elevenlabs_pronunciation_dictionary_id = selected_dictionary
            self.settings.elevenlabs_pronunciation_dictionary_version_id = selected_version

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

    def _set_json(self, key: str, value: object) -> None:
        self.env_path.touch(mode=0o600, exist_ok=True)
        set_key(
            str(self.env_path),
            key,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            quote_mode="always",
        )
        if os.name == "posix":
            self.env_path.chmod(0o600)


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")
