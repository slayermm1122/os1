# OS1

> A small voice agent for any handbook, manual, policy, or document set.

![OS1 homepage](https://github.com/slayermm1122/os1/blob/main/docs/assets/os1-home.jpg?raw=1)

OS1 is an open-source experiment in natural voice support: give an AI the material it should know, then talk to it as if it were a calm, fast, always-available teammate.

The goal is simple: turn static documents into a voice interface. Not a search box. Not a ticket form. A natural spoken answer, grounded in the material you provide.

OS1 is designed for scenes like:

- Internal AI assistants for company handbooks, SOPs, onboarding docs, and support runbooks
- Voice customer support for products, services, education, and local businesses
- Sales or pre-sales voice agents that answer from a known product manual
- Lightweight prototypes for document-based voice AI before building a full production stack

The interface is intentionally minimal: a warm orange-red room, one button, one voice.

## Version

Current version: `v0.01`

This is the first working prototype. It is deliberately small, inspectable, and easy to change.

## What Works In v0.01

- Browser voice recording with a press-to-talk button
- ElevenLabs speech-to-text for user audio
- xAI Grok text generation, currently defaulting to `grok-4.5`
- `reasoning_effort=low` for faster responses
- Streaming LLM output from the backend
- ElevenLabs streaming TTS through WebSocket
- Simple centered voice UI inspired by OS-style ambient assistants
- API key dialog in the UI
- Optional server-side `.env` fallback for local testing

## Current Limits

`v0.01` is not a full realtime voice agent yet.

The current flow is:

```text
User records audio
  -> browser uploads full recording
  -> ElevenLabs STT transcribes it
  -> Grok answers
  -> answer chunks are sent to ElevenLabs TTS
  -> browser plays the voice
```

This means there is still a pause after the user stops speaking. The output side is already partially streamed, but input speech is not yet realtime.

Document grounding is not fully productized yet. There is no document upload UI in `v0.01`.

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
- STT model: `scribe_v2`

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

Example:

```bash
LLM_API_KEY=...
LLM_BASE_URL=https://api.x.ai/v1
LLM_MODEL=grok-4.5
LLM_REASONING_EFFORT=low

ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=JBFqnCBsd6RMkjVDRZzb
```

`.env` is ignored by git. Do not commit real API keys.

## Security

OS1 is local-first research software. Read [SECURITY.md](SECURITY.md) before publishing, deploying, or sharing a hosted instance.

## Roadmap

### v0.02

Faster response loop.

The next version will move speech input toward realtime STT, so OS1 can begin understanding while the user is still speaking.

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
