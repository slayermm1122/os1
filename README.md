# OS1

> A small voice agent for any handbook, manual, policy, or document set.

![OS1 homepage](https://cdn.jsdelivr.net/gh/slayermm1122/os1@main/docs/assets/os1-home.jpg)

OS1 is an open-source experiment in natural voice support: give an AI the material it should know, then talk to it as if it were a calm, fast, always-available teammate.

The goal is simple: turn static documents into a voice interface. Not a search box. Not a ticket form. A natural spoken answer, grounded in the material you provide.

OS1 is designed for scenes like:

- Internal AI assistants for company handbooks, SOPs, onboarding docs, and support runbooks
- Voice customer support for products, services, education, and local businesses
- Sales or pre-sales voice agents that answer from a known product manual
- Lightweight prototypes for document-based voice AI before building a full production stack

The interface is intentionally minimal: a warm orange-red room, one button, one voice.

> [!IMPORTANT]
> OS1 is an independent, non-commercial toy project for research and technical exchange. It is not affiliated with, endorsed by, or connected to the film *Her*, Warner Bros., Annapurna Pictures, or any related rights holder. The name "OS1" in this repository refers only to this software experiment. All film titles, characters, and related intellectual property belong to their respective owners.

## Version

Current version: `v0.03.01`

This is still deliberately small, inspectable, and easy to change.

## What Works In v0.03.01

- Press-to-talk voice recording with browser PCM streaming
- ElevenLabs realtime speech-to-text while the user is still recording
- xAI Grok text generation, currently defaulting to `grok-4.5`
- `reasoning_effort=low` for faster responses
- Streaming LLM output from the backend
- ElevenLabs streaming TTS through WebSocket
- AudioContext playback for streamed PCM audio chunks
- Male / female voice selection in the UI
- Simple centered voice UI inspired by OS-style ambient assistants
- API key dialog in the UI
- Optional server-side `.env` fallback for local testing
- Capability-specific LLM, STT, TTS, and knowledge gateways
- Per-turn latency, usage, cost, cache, and error telemetry in local SQLite
- Startup readiness checks for the configured xAI model and ElevenLabs voice path
- Explicit `checking`, `transcribing`, `thinking`, and `speaking` interface states
- Structured provider errors with upstream status, official error detail, and request ID
- A dedicated read-only Knowledge Base page at `/knowledge` for inspecting compiled wiki pages and chunks
- A bundled Attention Is All You Need English knowledge corpus
- Parallel SQLite FTS5 chunk search and Grok wiki-page selection
- Cache-aware KV Conversation history with references appended to the latest user message
- Provider-neutral AI gateway operations for streaming text and structured objects
- An expandable Ref panel that exposes both `llm_search` wiki hits and `lex_search` chunk hits

## Current Limits

The UI is still intentionally simple: the user presses once to talk and presses again to stop. OS1 does not interrupt, barge in, or run a fully hands-free conversation loop yet.

The current flow is:

```text
Browser PCM
  -> backend WebSocket
  -> ElevenLabs realtime STT
  -> committed transcript
  -> parallel lexical and LLM wiki search
  -> references appended to the latest user message
  -> Grok streaming response
  -> ElevenLabs TTS WebSocket
  -> browser AudioContext playback
```

This reduces the wait after the user stops speaking because transcription has already been running during the recording.

Document upload, manual rebuild controls, and automatic compilation are intentionally disabled in the v0.03.01 UI and API. The bundled Attention Is All You Need PDF has hand-maintained JSONL and wiki artifacts so retrieval can be evaluated independently before the ingestion architecture is designed.

Knowledge search in v0.03.01 supports English documents and English questions only. Chinese tokenization and cross-language lexical retrieval are deferred to a later v0.03.x release.

Conversation history is currently short-lived and intentionally simple. The backend keeps the most recent eight turns in memory for one hour by default, and loses them when the process restarts. Telemetry persists individual turns by default unless explicitly disabled, but it is not a memory system and there is no persisted conversation entity above `turn_id` yet.

The startup readiness gate uses authenticated provider capability endpoints and does not generate text or audio. It verifies the current network path, key acceptance, and required realtime STT/TTS permissions; the live streaming request can still fail later if a provider changes state or the account runs out of credits.

## Versioning

OS1 uses `X.Y.Z` to describe the kind of change:

- `X` changes when the product identity or overall interface is substantially redesigned.
- `Y` changes for user-visible features, experience improvements, and fixes.
- `Z` changes for internal backend, architecture, and engineering upgrades.

`v0.02.01` is a `Z` release: the voice experience remains the same while the backend becomes modular and observable.

`v0.02.02` adds a startup provider readiness gate and a quiet thinking-state animation while Grok is preparing its first output.

`v0.02.03` makes that readiness gate compatible with restricted ElevenLabs keys and surfaces sanitized provider error details in the interface.

`v0.03.01` adds document knowledge, dual retrieval, and a cache-aware model conversation without adding a conversation-list product concept.

See [CHANGELOG.md](CHANGELOG.md) for the history of each release.

## What You Need

You need:

- Python 3.11+
- An xAI API key
- An ElevenLabs API key
- A browser with microphone permission

The default backend settings use:

- Brain: `xAI` / `grok-4.5`
- LLM search: `xAI` / `grok-4.5` with `reasoning_effort=low`
- Voice: `ElevenLabs`
- TTS model: `eleven_flash_v2_5`
- STT model: `scribe_v2_realtime`

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
```

Then open:

```text
http://127.0.0.1:8000
```

On first use, OS1 will ask for:

- xAI API key
- ElevenLabs API key

These are stored in browser `sessionStorage`, survive a page refresh, and are cleared when the tab session ends. They are sent only to your local backend for the request that needs them.

## Optional `.env`

For local development, you can also use `.env` as a server-side fallback.

```bash
cp .env.example .env
```

`.env` is ignored by git. Do not commit real API keys.

Telemetry is enabled for local development so every turn and provider stage can be inspected. Set `TELEMETRY_ENABLED=false` in your private `.env` only when full-content local recording is not acceptable.

The telemetry schema is OS1's own versioned layout for `data/telemetry.sqlite`, not an xAI or ElevenLabs schema. Schema v3 adds a `purpose` label so diagnostics can distinguish the answer-model call from the knowledge-selector call. Each turn records STT, answer LLM, TTS, the hybrid knowledge result, individual `lex_search` and `llm_search` outcomes, and the selector LLM call. Normal hits, misses, timeouts, cancellations, and failures are all retained. It has no effect while telemetry is disabled.

## Search Self-Verification

See the mandatory
[Knowledge Search Self-Verification SOP](docs/knowledge-search-self-verification.md)
for the complete human-labeling and review workflow.

The bundled Attention paper has five human-curated source golden cases and three
English paraphrases per case. Evaluate retrieval alone, without STT, answer
generation, or TTS:

```bash
.venv/bin/python -m backend.evals.knowledge_search --provider lex
.venv/bin/python -m backend.evals.knowledge_search --provider llm
```

Each provider is scored separately with Recall@5, precision, MRR, and nDCG@5.
Detailed runs are stored locally in the ignored `data/search_eval.sqlite`.

## Security

OS1 is local-first research software. Read [SECURITY.md](SECURITY.md) before publishing, deploying, or sharing a hosted instance.

OS1 v0.03.01 accepts loopback traffic only and is not a public deployment. Telemetry is enabled by default and stores full transcripts, AI responses, model request snapshots, and knowledge snippets in the ignored local file `data/telemetry.sqlite`. Set `TELEMETRY_ENABLED=false` when this local full-content record is not acceptable. Future uploaded PDFs and ingest status are already excluded from git, but upload is not exposed in this release.

## Roadmap

### v0.03.02

Start lexical search from committed realtime STT paragraphs, merge and deduplicate prefetched evidence, and avoid reinjecting evidence already present in KV Conversation.

### v0.04

More model choices.

The provider menus are already present in the UI. Future versions will add more brain and voice providers beyond xAI and ElevenLabs.

### v0.05

Continuous memory for a single-window AI.

OS1 will remain one continuous interface rather than becoming a list of separate chat threads. This release will introduce deliberate memory maintenance instead of sending an ever-growing transcript back to the model. The current state and design boundary are recorded in the [memory plan](docs/memory-plan.md); the retrieval, consolidation, forgetting, and correction rules remain open for the dedicated design phase.

## Project Shape

```text
backend/
  app.py        Application composition and lifecycle
  api/          HTTP, SSE, and WebSocket transport
  core/         Turn orchestration, KV Conversation, chunking, and errors
  gateways/     Replaceable AI, STT, TTS, and knowledge providers
  telemetry/    Async recorder and SQLite schema

frontend/
  index.html    Minimal voice interface

knowledge/
  raw/          Immutable source documents
  chunks/       Canonical JSONL retrieval corpus
  wiki/         LLM-maintained index and sourced pages
```

## License

License coming soon.

## Disclaimer

This project is provided for research, learning, and technical exchange only. Please evaluate safety, privacy, compliance, and model behavior carefully before using it in production or customer-facing environments.
