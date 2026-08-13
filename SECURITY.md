# Security Policy

OS1 is experimental, local-first research software. Its current security model assumes one trusted user on one machine. It is not designed for public hosting, shared computers, untrusted local users, or multi-user access.

## The security boundary

OS1 must run on the loopback interface:

```bash
.venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

The backend rejects non-loopback clients and Host headers, rejects cross-origin HTTP requests, and requires same-origin WebSocket connections. Changing the bind address does not make OS1 safe for a LAN or the public internet; a hosted version would need authentication, authorization, TLS, CSRF protection, per-user isolation, secret management, and a separate deployment review.

## API keys and settings

- Provider keys are read by the local backend from `.env`.
- The browser has no API-key input and does not receive provider secrets from the backend.
- Provider credentials supplied through browser request payloads or headers are ignored by the active local flow.
- The homepage clears browser storage left by older OS1 versions. The current frontend stores only the selected motion style in local storage.
- Persona, model, language, voice, listening, and pronunciation settings can be written to `.env` through loopback-only settings endpoints.
- `.env` is ignored by Git, but this does not protect a file uploaded manually or copied elsewhere.

Never paste real keys into issues, pull requests, screenshots, recordings, or logs. If a key is exposed, revoke it in the provider dashboard and issue a replacement.

Use restricted provider keys where available. The ElevenLabs key requires realtime STT and WebSocket TTS; `voices_read` and `user_read` are needed only for the full voice-library and account display.

## What data goes where

### In the browser

The browser captures microphone audio and streams PCM to the local backend. It receives live transcripts, model output, synthesized audio, caption timing, provider status, and usage summaries so the interface can work. The page is served with a restrictive Content Security Policy, no-referrer policy, clickjacking protection, MIME sniffing protection, and a microphone-only Permissions Policy.

Anything visible on screen can still be captured by browser extensions, screen-recording software, accessibility tools, or another person with access to the machine.

### In local memory

Current active-session context is kept in backend process memory. It is cleared when the process restarts and expires after the configured idle session lifetime.

### In local SQLite telemetry

Telemetry is enabled by default and stored in `data/telemetry.sqlite`. OS1 creates the database automatically. New records contain operational data such as:

- turn, event, provider, model, status, and timing identifiers;
- token, cache, reasoning, audio-duration, byte, and character counts;
- selected voice IDs and provider request or trace IDs;
- sanitized error details and stack traces.

New telemetry records deliberately exclude user transcripts, prompts, conversation history, model responses, and TTS input text. The schema retains legacy content columns for database compatibility, but current writes leave them empty. Databases created by older OS1 versions may still contain historical content.

The local conversation history panel is backed by `data/chat_history.jsonl`. This append-only file contains user transcripts and complete model replies, including replies from interrupted turns. Protect or remove it separately when handling sensitive conversations.

Telemetry does not intentionally store API keys, request headers, or raw audio. It is diagnostic data, not an anonymity system: timestamps, model names, voice IDs, request IDs, error details, and usage patterns may still be sensitive.

On POSIX systems, the default telemetry directory is restricted to mode `0700` and the database plus SQLite sidecars to `0600`. These permissions reduce access by other local accounts but do not provide encryption at rest.

To stop new telemetry records, set:

```dotenv
TELEMETRY_ENABLED=false
```

To remove telemetry, stop OS1 and delete `data/telemetry.sqlite` together with any `-wal` and `-shm` sidecar files. Use your operating system's secure-deletion and full-disk-encryption features when your threat model requires them.

### At external providers

Voice audio and transcripts are processed by ElevenLabs, model prompts and conversation context are processed by the selected language-model provider, and generated response text is sent to ElevenLabs for speech synthesis. Provider logging, retention, training, regional processing, and deletion policies are controlled by those services and your account settings—not by OS1.

Review each provider's current privacy terms before using sensitive material. ElevenLabs logging is enabled by default for ordinary account compatibility; eligible Enterprise Zero Retention accounts can set `ELEVENLABS_ENABLE_LOGGING=false`.

## Built-in safeguards

OS1 currently includes:

- loopback client and Host enforcement;
- same-origin HTTP and WebSocket checks;
- request-body, recording, chat, and TTS size limits;
- per-client API rate limiting with bounded in-memory state;
- session expiry and maximum session count;
- upstream connection, read, write, pool, stream, and readiness timeouts;
- disabled OpenAPI, Swagger, and ReDoc endpoints;
- security response headers and a restrictive browser content policy;
- provider-error and credential redaction;
- private permissions for the default telemetry files.

These controls reduce accidental exposure; they are not a production security boundary. OS1 does not currently provide authentication, encrypted local storage, sandboxed provider plugins, per-user authorization, or independent audit logging.

## Safe use checklist

- Keep the server bound to `127.0.0.1`.
- Keep `.env`, `data/`, logs, screenshots, and recordings out of Git and public support threads.
- Use provider keys with the narrowest practical permissions and spending limits.
- Keep Python dependencies, the browser, and the operating system updated.
- Use a trusted machine with disk encryption and a locked user session.
- Treat provider-generated content and spoken actions as untrusted until verified.
- Do not use OS1 for high-stakes medical, legal, financial, safety-critical, or autonomous operational decisions.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the repository maintainer. Do not open a public issue containing credentials, private conversations, provider request IDs, database files, stack traces, or reproduction data that could identify a user.

Include a concise description, affected version or commit, reproduction steps with dummy data, and the likely impact. Rotate any credential used during testing before sharing the report.
