# OS1

> A small voice agent you can talk to like a calm, fast, always-available teammate.

![OS1 homepage](https://cdn.jsdelivr.net/gh/slayermm1122/os1@main/docs/assets/os1-home.jpg)

OS1 is an open-source experiment in natural voice support: one warm room, one button, one voice.

The goal is simple: make talking to a model feel easy and intimate. Not a search box. Not a ticket form. A short spoken answer, in a continuous conversation.

OS1 is designed for scenes like:

- Personal voice companions and calm desk assistants
- Lightweight prototypes for voice AI before building a full production stack
- Local experiments with realtime speech-to-text, LLM replies, and streaming speech

The interface is intentionally minimal: a warm orange-red room, one orb, live conversation bubbles.

> [!IMPORTANT]
> OS1 is an independent, non-commercial toy project for research and technical exchange. It is not affiliated with, endorsed by, or connected to the film *Her*, Warner Bros., Annapurna Pictures, or any related rights holder. The name "OS1" in this repository refers only to this software experiment. All film titles, characters, and related intellectual property belong to their respective owners.

## Version

Current version: `v0.03.03`

This is still deliberately small, inspectable, and easy to change.

## What Works In v0.03.03

- Tap-to-speak voice recording with browser PCM streaming
- ElevenLabs realtime speech-to-text while you are still speaking
- **VAD auto end-of-speech**: pause briefly to finish; no 15-second hard stop or countdown
- xAI Grok text generation, currently defaulting to `grok-4.5`
- `reasoning_effort=low` for faster responses
- Streaming LLM output from the backend
- ElevenLabs streaming TTS through WebSocket (`eleven_flash_v2_5`)
- AudioContext playback for streamed PCM audio chunks
- Mic-reactive orb bars while you speak; restrained motion while OS1 replies
- Chat bubbles in the center panel: you on the right, OS1 on the left
- Soft voice UI with a quieter Keys / Voice settings language
- Optional English assistant name in Voice settings
- Male / female voice selection and optional custom ElevenLabs voice IDs
- API key dialog in the UI
- Optional server-side `.env` fallback for local testing
- Capability-specific LLM, STT, and TTS gateways
- Per-turn latency, usage, cost, cache, and error telemetry in local SQLite
- Startup readiness checks for the configured xAI model and ElevenLabs voice path
- Explicit `checking`, `listening`, `thinking`, and `speaking` interface states
- Structured provider errors with upstream status, official error detail, and request ID
- Short spoken-chat system instructions so replies stay brief and TTS-friendly

## Current Limits

OS1 does not interrupt, barge in, or run a fully hands-free continuous loop yet. One tap starts listening; silence ends the turn.

The current flow is:

```text
Browser PCM
  -> backend WebSocket
  -> ElevenLabs realtime STT (VAD commit on pause)
  -> Grok streaming response
  -> ElevenLabs TTS WebSocket
  -> browser AudioContext playback
```

Transcription runs while you speak, so the wait after you pause stays short.

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

`v0.03.02` synchronizes live captions to ElevenLabs STT/TTS timestamps and the browser audio output clock.

`v0.03.03` moves the voice loop to VAD end-of-speech, chat bubbles, quieter settings, and short spoken replies.

See [CHANGELOG.md](CHANGELOG.md) for the history of each release.

## What You Need

You need:

- Python 3.11+
- An xAI API key
- An ElevenLabs API key
- A browser with microphone permission

The default backend settings use:

- Brain: `xAI` / `grok-4.5`
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

## Security

OS1 is local-first research software. Read [SECURITY.md](SECURITY.md) before publishing, deploying, or sharing a hosted instance.

OS1 v0.03.03 accepts loopback traffic only and is not a public deployment. Telemetry is enabled by default and stores full transcripts, AI responses, and model request snapshots in the ignored local file `data/telemetry.sqlite`. Set `TELEMETRY_ENABLED=false` when this local full-content record is not acceptable.

## Roadmap

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
  core/         Turn orchestration, sessions, and errors
  gateways/     Replaceable AI, STT, and TTS providers
  telemetry/    Async recorder and SQLite schema

frontend/
  index.html    Minimal voice interface
```

## Legal

This is a personal research project. It is not a commercial product and is not endorsed by any film studio or related rights holder.
