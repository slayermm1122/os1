from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")


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


@dataclass(frozen=True)
class Settings:
    root_dir: Path = ROOT_DIR
    frontend_dir: Path = ROOT_DIR / "frontend"

    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")
    elevenlabs_voice_id: str = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    elevenlabs_stt_model: str = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")
    elevenlabs_tts_model: str = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")
    elevenlabs_output_format: str = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")
    elevenlabs_stream_output_format: str = os.getenv("ELEVENLABS_STREAM_OUTPUT_FORMAT", "pcm_16000")
    elevenlabs_stt_language_code: str = os.getenv("ELEVENLABS_STT_LANGUAGE_CODE", "")

    llm_api_key: str = (
        os.getenv("LLM_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("XAI_API_KEY")
        or ""
    )
    llm_base_url: str = os.getenv(
        "LLM_BASE_URL",
        "https://api.x.ai/v1",
    )
    llm_model: str = os.getenv("LLM_MODEL", "grok-4.5")
    llm_reasoning_effort: str = os.getenv("LLM_REASONING_EFFORT", "low")
    llm_temperature: float = _float_env("LLM_TEMPERATURE", 0.4)
    llm_max_tokens: int = _int_env("LLM_MAX_TOKENS", 700)

    system_prompt: str = os.getenv(
        "SYSTEM_PROMPT",
        "你是一个语音客服 agent。回答要简短、自然、准确；如果参考资料不足，直接说明不确定。",
    )
    max_history_turns: int = _int_env("MAX_HISTORY_TURNS", 8)

    knowledge_enabled: bool = _bool_env("KNOWLEDGE_ENABLED", False)
    knowledge_docs_dir: Path = ROOT_DIR / os.getenv("KNOWLEDGE_DOCS_DIR", "knowledge_docs")
    knowledge_db_path: Path = ROOT_DIR / os.getenv("KNOWLEDGE_DB_PATH", "data/knowledge.sqlite")
    knowledge_limit: int = _int_env("KNOWLEDGE_LIMIT", 4)

    max_upload_bytes: int = _int_env("MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
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

    @property
    def llm_chat_url(self) -> str:
        return f"{self.llm_base_url.rstrip('/')}/chat/completions"

    @property
    def tts_media_type(self) -> str:
        if self.elevenlabs_output_format.startswith("mp3"):
            return "audio/mpeg"
        if self.elevenlabs_output_format.startswith("wav"):
            return "audio/wav"
        if self.elevenlabs_output_format.startswith("pcm"):
            return "audio/L16"
        return "application/octet-stream"

    @property
    def tts_stream_media_type(self) -> str:
        if self.elevenlabs_stream_output_format.startswith("pcm"):
            return "audio/L16"
        if self.elevenlabs_stream_output_format.startswith("mp3"):
            return "audio/mpeg"
        if self.elevenlabs_stream_output_format.startswith("wav"):
            return "audio/wav"
        return "application/octet-stream"

    @property
    def tts_stream_sample_rate(self) -> int | None:
        if not self.elevenlabs_stream_output_format.startswith("pcm_"):
            return None
        try:
            return int(self.elevenlabs_stream_output_format.split("_", 1)[1])
        except (IndexError, ValueError):
            return None


settings = Settings()
