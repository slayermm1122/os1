from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

DEFAULT_MALE_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
DEFAULT_FEMALE_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _string_env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _string_list_env(name: str) -> tuple[str, ...]:
    value = os.getenv(name, "").strip()
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item).strip() for item in parsed if str(item).strip())


@dataclass
class Settings:
    root_dir: Path = ROOT_DIR
    frontend_dir: Path = ROOT_DIR / "frontend"

    telemetry_enabled: bool = _bool_env("TELEMETRY_ENABLED", True)
    telemetry_db_path: Path = ROOT_DIR / os.getenv("TELEMETRY_DB_PATH", "data/telemetry.sqlite")
    telemetry_queue_size: int = _int_env("TELEMETRY_QUEUE_SIZE", 2048)
    enforce_local_access: bool = True

    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")
    elevenlabs_voice_id: str = os.getenv("ELEVENLABS_VOICE_ID", DEFAULT_MALE_VOICE_ID)
    assistant_response_language: str = _string_env(
        "ASSISTANT_RESPONSE_LANGUAGE",
        "auto",
    ).lower()
    elevenlabs_male_voice_id: str = os.getenv("ELEVENLABS_MALE_VOICE_ID", DEFAULT_MALE_VOICE_ID)
    elevenlabs_female_voice_id: str = os.getenv("ELEVENLABS_FEMALE_VOICE_ID", DEFAULT_FEMALE_VOICE_ID)
    elevenlabs_stt_model: str = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")
    elevenlabs_realtime_stt_model: str = os.getenv(
        "ELEVENLABS_REALTIME_STT_MODEL",
        "scribe_v2_realtime",
    )
    elevenlabs_realtime_stt_audio_format: str = os.getenv(
        "ELEVENLABS_REALTIME_STT_AUDIO_FORMAT",
        "pcm_16000",
    )
    # VAD = Voice Activity Detection. "vad" auto-commits after silence; "manual" waits for client stop.
    elevenlabs_stt_commit_strategy: str = _string_env(
        "ELEVENLABS_STT_COMMIT_STRATEGY",
        "vad",
    ).lower()
    elevenlabs_stt_vad_threshold: float = min(
        max(_float_env("ELEVENLABS_STT_VAD_THRESHOLD", 0.4), 0.0),
        1.0,
    )
    elevenlabs_stt_vad_silence_threshold_secs: float = min(
        max(_float_env("ELEVENLABS_STT_VAD_SILENCE_THRESHOLD_SECS", 1.2), 0.1),
        10.0,
    )
    elevenlabs_stt_vad_min_speech_duration_ms: int = max(
        _int_env("ELEVENLABS_STT_VAD_MIN_SPEECH_DURATION_MS", 100),
        0,
    )
    elevenlabs_stt_vad_min_silence_duration_ms: int = max(
        _int_env("ELEVENLABS_STT_VAD_MIN_SILENCE_DURATION_MS", 100),
        0,
    )
    elevenlabs_tts_model: str = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")
    elevenlabs_output_format: str = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")
    elevenlabs_stream_output_format: str = os.getenv("ELEVENLABS_STREAM_OUTPUT_FORMAT", "pcm_16000")
    elevenlabs_stt_language_code: str = os.getenv("ELEVENLABS_STT_LANGUAGE_CODE", "")
    elevenlabs_stt_keyterms: tuple[str, ...] = _string_list_env(
        "ELEVENLABS_STT_KEYTERMS_JSON"
    )
    elevenlabs_pronunciation_dictionary_id: str = os.getenv(
        "ELEVENLABS_PRONUNCIATION_DICTIONARY_ID",
        "",
    )
    elevenlabs_pronunciation_dictionary_version_id: str = os.getenv(
        "ELEVENLABS_PRONUNCIATION_DICTIONARY_VERSION_ID",
        "",
    )
    elevenlabs_enable_logging: bool = _bool_env("ELEVENLABS_ENABLE_LOGGING", True)
    default_tts_provider: str = os.getenv("TTS_PROVIDER", "elevenlabs")
    assistant_name: str = _string_env("ASSISTANT_NAME", "")
    user_name: str = _string_env("USER_NAME", "")
    assistant_persona: str = _string_env("ASSISTANT_PERSONA", "default").lower()

    llm_api_key: str = (
        os.getenv("LLM_API_KEY")
        or os.getenv("XAI_API_KEY")
        or ""
    )
    llm_provider: str = _string_env("LLM_PROVIDER", "xai").lower()
    llm_base_url: str = os.getenv(
        "LLM_BASE_URL",
        "https://api.x.ai/v1",
    )
    llm_model: str = os.getenv("LLM_MODEL", "grok-4.5")
    llm_reasoning_effort: str = os.getenv("LLM_REASONING_EFFORT", "low")
    llm_temperature: float = _float_env("LLM_TEMPERATURE", 0.4)
    llm_max_tokens: int = _int_env("LLM_MAX_TOKENS", 700)

    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    deepseek_thinking: str = _string_env("DEEPSEEK_THINKING", "disabled").lower()

    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_base_url: str = os.getenv(
        "GEMINI_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta",
    )
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    gemini_thinking_level: str = _string_env("GEMINI_THINKING_LEVEL", "minimal").lower()
    google_ai_project: str = os.getenv("GOOGLE_AI_PROJECT", "")
    google_ai_project_number: str = os.getenv("GOOGLE_AI_PROJECT_NUMBER", "")

    system_prompt: str = os.getenv(
        "SYSTEM_PROMPT",
        (
            "You are OS1, a concise voice assistant in a live voice conversation.\n"
            "Every response is sent directly to text-to-speech. Write only the words that should "
            "be spoken aloud.\n"
            "Answer the user directly in the required conversational language. Start with the answer. "
            "Use short, complete sentences and ordinary punctuation. Most replies should be one "
            "to three sentences unless the user asks for detail.\n"
            "Before responding, rewrite anything that would sound awkward when read aloud. Spell "
            "out numbers, ordinals, dates, times, currencies, percentages, measurements, "
            "abbreviations, keyboard shortcuts, symbols, and URLs in natural spoken form. Expand "
            "ambiguous abbreviations. Prefer familiar words and contractions.\n"
            "Use periods, commas, and question marks to create a calm natural rhythm. Avoid "
            "excessive ellipses, repeated punctuation, and all-caps emphasis.\n"
            "Never output markdown, headings, bullet points, numbered lists, tables, code blocks, "
            "raw code, XML, SSML, audio tags, stage directions, emojis, citations, or decorative "
            "symbols. Do not describe how the response should sound. Do not include any text that "
            "is not meant to be spoken."
        ),
    )

    max_upload_bytes: int = _int_env("MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
    max_http_body_bytes: int = _int_env("MAX_HTTP_BODY_BYTES", 30 * 1024 * 1024)
    max_chat_chars: int = _int_env("MAX_CHAT_CHARS", 8000)
    max_tts_chars: int = _int_env("MAX_TTS_CHARS", 4000)
    max_sessions: int = _int_env("MAX_SESSIONS", 200)
    session_ttl_seconds: int = _int_env("SESSION_TTL_SECONDS", 3600)
    rate_limit_requests: int = _int_env("RATE_LIMIT_REQUESTS", 120)
    rate_limit_window_seconds: int = _int_env("RATE_LIMIT_WINDOW_SECONDS", 60)
    upstream_connect_timeout_seconds: float = _float_env("UPSTREAM_CONNECT_TIMEOUT_SECONDS", 10.0)
    upstream_read_timeout_seconds: float = _float_env("UPSTREAM_READ_TIMEOUT_SECONDS", 90.0)
    upstream_write_timeout_seconds: float = _float_env("UPSTREAM_WRITE_TIMEOUT_SECONDS", 30.0)
    upstream_pool_timeout_seconds: float = _float_env("UPSTREAM_POOL_TIMEOUT_SECONDS", 10.0)
    upstream_stream_timeout_seconds: float = _float_env("UPSTREAM_STREAM_TIMEOUT_SECONDS", 120.0)
    provider_check_timeout_seconds: float = _float_env("PROVIDER_CHECK_TIMEOUT_SECONDS", 6.0)

    @property
    def llm_chat_url(self) -> str:
        return f"{self.llm_base_url.rstrip('/')}/chat/completions"

    @property
    def deepseek_chat_url(self) -> str:
        return f"{self.deepseek_base_url.rstrip('/')}/chat/completions"

    @property
    def gemini_interactions_url(self) -> str:
        return f"{self.gemini_base_url.rstrip('/')}/interactions"

settings = Settings()
