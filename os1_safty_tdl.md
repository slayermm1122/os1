# OS1 Security TDL

This file tracks security work that is intentionally deferred beyond the local-only `v0.02.02` prototype. It is not a claim that OS1 is production-ready.

## Release Boundary

- `v0.02.02` is local-only. HTTP and WebSocket traffic must come from loopback, use a loopback Host, and WebSocket browser traffic must be same-origin.
- Public, LAN, reverse-proxy, and multi-user deployments are unsupported until application authentication is implemented.
- Full-content telemetry is opt-in and disabled by default.

## Completed In v0.02.01

| Finding | Resolution |
| --- | --- |
| P1.1 | Reject non-local Host/client and cross-origin browser requests; reject WebSockets before `accept()`. |
| P1.4 | Telemetry defaults to disabled; documentation requires explicit consent. |
| P1.5 | Telemetry directory is `0700`; database, WAL, and SHM are `0600` on POSIX. |
| P2.1 | API keys moved from `localStorage` to tab-scoped `sessionStorage`; legacy keys are migrated and removed. |
| P2.3 | Playback events require the active `turn_id` and prior server audio. |
| P2.4 | Redaction covers generic Bearer/Authorization credentials and JSON header forms. |
| P2.5 | Session IDs have 128-character limits; JSON fields and declared HTTP body size are bounded. |
| P2.6 | Expired rate-limit clients are pruned and in-memory client cardinality is capped. |
| P2.7 | Security headers added; FastAPI docs/OpenAPI disabled; health output reduced. |
| P2.9 | `.env.*` is ignored while `.env.example` remains tracked. |
| P2.10 | Default knowledge index and document directories use private POSIX permissions. |
| P2.11 | Custom telemetry paths secure OS1 files without changing an existing parent directory. |
| P3.1 | Telemetry dynamic updates use per-table column allowlists. |
| P3.2 | Non-object WebSocket JSON is rejected as a protocol error. |
| P3.4 | Security boundary regression tests added. |

## Required Before Public Deployment

### P1.2 Application Authentication

Design one authentication model that works for HTTP, SSE, and WebSocket before allowing non-loopback traffic. It must include:

- Per-user or per-installation authentication, not one shared URL token.
- Separate admin authorization for knowledge reindex and future telemetry deletion.
- CSRF/origin handling appropriate to the selected credential transport.
- Per-principal quotas and auditable key ownership.
- Reverse-proxy-aware trusted proxy configuration.

Do not add an `ALLOW_REMOTE=true` escape hatch without these controls.

### P1.3 Provider Retention

- ElevenLabs: set `ELEVENLABS_ENABLE_LOGGING=false` only for accounts with Zero Retention Mode enabled. The option is wired into STT and TTS, but ordinary accounts may not support it.
- xAI: ZDR is enabled at the enterprise team level and requires no request-body parameter. Add telemetry for the `x-zero-data-retention` response header when a production privacy dashboard exists.
- Production documentation must state provider retention separately from OS1 local telemetry retention.

### P1.4 Telemetry Lifecycle

Before a hosted dashboard or multi-user deployment:

- Define retention by days and/or maximum database size.
- Add automatic pruning and vacuum policy with bounded impact on the voice path.
- Add per-session and all-data deletion workflows covering DB, WAL, SHM, backups, and exported diagnostics.
- Decide whether full prompts/history are necessary or whether sampled/hashed/redacted fields are sufficient.
- Evaluate SQLCipher or platform keychain-backed envelope encryption for data at rest.

## Important Follow-Ups

### P2.2 Session And Data Deletion

Add explicit "New session", "Delete this session", and "Delete local telemetry" operations. UI clearing must state whether it only changes the screen or also deletes backend data.

### P2.5 Streaming Body Limits

The current middleware rejects oversized declared `Content-Length`, and field/upload readers have limits. Add an ASGI receive wrapper before public deployment to enforce total bytes for chunked requests that omit or lie about `Content-Length`.

### P2.6 Distributed Rate Limits

The local limiter now prunes and caps state. Public or multi-process deployment requires a shared limiter, separate concurrent WebSocket limits, and per-user/provider-budget quotas.

### P2.7 Strict CSP And Public Health Model

- Move inline CSS/JavaScript to static files or add per-response nonces so CSP can remove `'unsafe-inline'`.
- Split public liveness from authenticated diagnostics; do not expose provider/key/config state publicly.
- Review COEP/CORP only when required by browser audio dependencies.

### P2.8 Supply Chain And GitHub

- Add a reproducible lock file with exact versions and hashes.
- Enable Dependabot alerts and security updates.
- Protect `main` and require review/status checks.
- Restrict GitHub Actions to trusted publishers and pin third-party actions by full commit SHA.
- Keep secret scanning and push protection enabled.

### Custom Storage Paths

OS1 only manages directory permissions for its default `data/` and `knowledge_docs/` locations, or for a dedicated directory it creates itself. Users who point telemetry or knowledge settings at an existing custom directory are responsible for making that directory private. OS1 still applies `0600` to SQLite files it owns on POSIX.

### P3.3 Developer Tooling

Keep local `pip` at `26.1.2` or newer. This is a development environment concern, not an OS1 runtime dependency.

## Review Trigger

Re-run the security review before any of these changes:

- Binding outside loopback.
- Adding authentication or a reverse proxy.
- Enabling knowledge upload/reindex in the UI.
- Adding a telemetry dashboard or deletion API.
- Adding a new LLM, STT, or TTS provider.
- Deploying for multiple users or processing regulated data.
