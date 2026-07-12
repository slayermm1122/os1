SCHEMA_VERSION = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms REAL,
    user_text TEXT,
    assistant_text TEXT,
    response_complete INTEGER NOT NULL DEFAULT 0,
    failed_stage TEXT,
    error_id TEXT
);

CREATE TABLE IF NOT EXISTS turn_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    stage TEXT,
    occurred_at TEXT NOT NULL,
    offset_ms REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS llm_calls (
    call_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning_effort TEXT,
    purpose TEXT NOT NULL DEFAULT 'answer',
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms REAL,
    first_token_ms REAL,
    request_json TEXT NOT NULL,
    response_text TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cached_tokens INTEGER,
    reasoning_tokens INTEGER,
    cache_status TEXT,
    cost_usd_ticks INTEGER,
    finish_reason TEXT,
    provider_request_id TEXT,
    system_fingerprint TEXT,
    service_tier TEXT,
    error_id TEXT
);

CREATE TABLE IF NOT EXISTS stt_calls (
    call_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms REAL,
    sample_rate INTEGER,
    audio_bytes INTEGER NOT NULL DEFAULT 0,
    audio_duration_ms REAL,
    partial_count INTEGER NOT NULL DEFAULT 0,
    committed_count INTEGER NOT NULL DEFAULT 0,
    first_partial_ms REAL,
    commit_latency_ms REAL,
    transcript_text TEXT,
    language_code TEXT,
    provider_request_id TEXT,
    error_id TEXT
);

CREATE TABLE IF NOT EXISTS tts_calls (
    call_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    voice_id TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms REAL,
    input_text TEXT NOT NULL DEFAULT '',
    input_chars INTEGER NOT NULL DEFAULT 0,
    input_chunks INTEGER NOT NULL DEFAULT 0,
    first_text_ms REAL,
    first_audio_ms REAL,
    audio_bytes INTEGER NOT NULL DEFAULT 0,
    audio_duration_ms REAL,
    output_format TEXT,
    sample_rate INTEGER,
    character_cost INTEGER,
    provider_request_id TEXT,
    trace_id TEXT,
    error_id TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_calls (
    call_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms REAL,
    query_text TEXT NOT NULL,
    outcome TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    results_json TEXT NOT NULL DEFAULT '[]',
    error_id TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    error_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
    call_id TEXT,
    stage TEXT NOT NULL,
    provider TEXT,
    code TEXT NOT NULL,
    retryable INTEGER NOT NULL,
    upstream_status INTEGER,
    provider_request_id TEXT,
    public_message TEXT NOT NULL,
    technical_message TEXT NOT NULL,
    exception_type TEXT NOT NULL,
    stack_trace TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    offset_ms REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_started_at ON turns(started_at);
CREATE INDEX IF NOT EXISTS idx_turns_status ON turns(status);
CREATE INDEX IF NOT EXISTS idx_events_turn_name ON turn_events(turn_id, name);
CREATE INDEX IF NOT EXISTS idx_llm_turn ON llm_calls(turn_id);
CREATE INDEX IF NOT EXISTS idx_stt_turn ON stt_calls(turn_id);
CREATE INDEX IF NOT EXISTS idx_tts_turn ON tts_calls(turn_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_turn ON knowledge_calls(turn_id);
CREATE INDEX IF NOT EXISTS idx_errors_turn_stage ON errors(turn_id, stage);
"""
