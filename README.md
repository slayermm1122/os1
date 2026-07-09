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

Current version: `v0.02`

This is still deliberately small, inspectable, and easy to change.

## What Works In v0.02

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

## Current Limits

The UI is still intentionally simple: the user presses once to talk and presses again to stop. OS1 does not interrupt, barge in, or run a fully hands-free conversation loop yet.

The current flow is:

```text
Browser PCM
  -> backend WebSocket
  -> ElevenLabs realtime STT
  -> committed transcript
  -> Grok streaming response
  -> ElevenLabs TTS WebSocket
  -> browser AudioContext playback
```

This reduces the wait after the user stops speaking because transcription has already been running during the recording.

Document grounding is not fully productized yet. There is no document upload UI in `v0.02`.

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

These are stored in browser `localStorage` and sent only to your local backend for the request that needs them.

## Optional `.env`

For local development, you can also use `.env` as a server-side fallback.

```bash
cp .env.example .env
```

`.env` is ignored by git. Do not commit real API keys.

## Security

OS1 is local-first research software. Read [SECURITY.md](SECURITY.md) before publishing, deploying, or sharing a hosted instance.

## Roadmap

### v0.03

Document upload.

Users will be able to upload text documents directly in the UI and let OS1 answer from those materials.

### v0.04

More model choices.

The provider menus are already present in the UI. Future versions will add more brain and voice providers beyond xAI and ElevenLabs.

## Project Shape

```text
backend/
  app.py        FastAPI routes and streaming orchestration
  voice.py      ElevenLabs STT/TTS client
  llm.py        Grok/OpenAI-compatible streaming client
  knowledge.py  Document context module

frontend/
  index.html    Minimal voice interface
```

## License

License coming soon.

## Disclaimer

This project is provided for research, learning, and technical exchange only. Please evaluate safety, privacy, compliance, and model behavior carefully before using it in production or customer-facing environments.
