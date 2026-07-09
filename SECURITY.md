# Security Policy

OS1 is an early local-first prototype. Treat it as research software unless you add your own production controls.

## API Keys

- Do not commit `.env`.
- Do not paste real API keys into issues, pull requests, screenshots, or logs.
- `.env` is ignored by git, but `.gitignore` does not protect you if you upload files manually through the GitHub web UI.
- If a key is ever committed, shared, logged, or pasted into an untrusted place, revoke it in the provider dashboard and create a new one.

The web UI can store user-provided xAI and ElevenLabs keys in browser `localStorage`. This is convenient for local testing, but it is not appropriate for shared, public, or hostile browser environments.

Use the API Keys dialog to clear browser-stored keys when you are done testing. This does not remove keys from a local `.env` fallback file.

## Local Use

The default development command binds to `127.0.0.1`:

```bash
uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
```

Do not expose the app publicly with `--host 0.0.0.0` unless you add authentication, HTTPS, rate limits, logging policy, and deployment-specific secret management.

## Built-In Guardrails

The prototype includes basic limits:

- Upload size limit
- Text length limits
- In-memory session TTL and maximum session count
- Basic per-IP API request rate limit
- Upstream request timeouts
- Redacted user-facing upstream errors

These are not a complete security boundary. They are intended to reduce accidental misuse during local development.

## Reporting Issues

For now, please report security issues privately to the repository maintainer rather than opening a public issue with sensitive details.
