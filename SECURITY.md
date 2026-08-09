# Security Policy

OS1 is an early local-first prototype. Treat it as research software unless you add your own production controls.

## API Keys

- Do not commit `.env`.
- Do not paste real API keys into issues, pull requests, screenshots, or logs.
- `.env` is ignored by git, but `.gitignore` does not protect you if you upload files manually through the GitHub web UI.
- If a key is ever committed, shared, logged, or pasted into an untrusted place, revoke it in the provider dashboard and create a new one.

Provider keys are read only by the local backend from `.env`. The frontend has no API-key input, does not accept provider credentials through request headers, and does not use browser storage. The homepage also sends `Clear-Site-Data: "storage"` to remove values left by older OS1 versions.

Voice selection and the small Voice profile settings are written to `.env` only through the loopback-only backend. The UI never receives the contents of `.env` or any provider key.

## Telemetry And Privacy

Telemetry is enabled by default for this local research project. It is stored in `data/telemetry.sqlite` and includes full user transcripts, AI responses, and model request snapshots such as prompts and conversation history. It does not intentionally store API keys, request headers, or raw audio. Set `TELEMETRY_ENABLED=false` when full-content local recording is not acceptable.

The database is ignored by git, but it still contains private conversation content. Do not publish, attach, or share it. To clear telemetry, stop OS1 and delete `data/telemetry.sqlite`, `data/telemetry.sqlite-wal`, and `data/telemetry.sqlite-shm`.

On POSIX systems, OS1 enforces mode `0700` on the telemetry directory and `0600` on the database and SQLite sidecars. This protects against other local accounts but is not encryption at rest.

Provider retention is separate from OS1 telemetry. ElevenLabs logging remains enabled for compatibility with ordinary accounts unless an Enterprise Zero Retention account sets `ELEVENLABS_ENABLE_LOGGING=false`. xAI ZDR is enabled at the team level for eligible Enterprise accounts.

## Local Use

The default development command binds to `127.0.0.1`:

```bash
uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
```

OS1 v0.03 enforces loopback clients, loopback Host headers, and same-origin WebSocket requests. It is intentionally unusable as a public or LAN service. Public deployment requires a separate authenticated architecture; changing only the Uvicorn bind address is not sufficient.

## Built-In Guardrails

The prototype includes basic limits:

- Upload size limit
- Text length limits
- In-memory session TTL and maximum session count
- Basic per-IP API request rate limit
- Loopback client/Host enforcement and same-origin browser request checks
- Security response headers and disabled API documentation endpoints
- Upstream request timeouts
- Redacted user-facing upstream errors

These are not a complete security boundary. They are intended to reduce accidental misuse during local development.

## Reporting Issues

For now, please report security issues privately to the repository maintainer rather than opening a public issue with sensitive details.
