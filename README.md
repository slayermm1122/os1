# OS1

**A local-first voice companion built for continuous, natural conversation.**

OS1 replaces the usual chat box with a single live voice surface. Start once, speak naturally, pause when you are done, and hear the reply as it is generated. The interface stays deliberately quiet so the conversation—not the machinery—remains the focus.

![OS1 live voice interface](https://cdn.jsdelivr.net/gh/slayermm1122/os1@4a681937965e1e941a65e26b46305b6a6d87f294/docs/assets/os1-home.jpg)

## Why OS1

- **Natural conversation.** OS1 keeps one live session open, detects when you finish speaking, streams each reply end to end, and lets you interrupt naturally.
- **A choice of brains.** Switch between Grok, DeepSeek, and Gemini from the interface; the next response uses the newly selected model.
- **Name your own AI.** Give your assistant a name, tell it yours, and choose a default, concise, or conversational response style.
- **A voice you can shape.** Choose from ElevenLabs voices, set the reply language, tune listening sensitivity, and teach OS1 uncommon terms or spoken aliases.
- **Choose its look.** Pick from eight responsive orb motions, each giving your AI a different visual character.
- **Local control.** Provider keys and core settings stay on your machine; secrets are never returned to the browser.

![OS1 motion selection](https://cdn.jsdelivr.net/gh/slayermm1122/os1@dd887547917f109d9877d37298281f88beb11045/docs/assets/os1-motion.jpg)

## Quick start

### Requirements

- Python 3.11+
- An [ElevenLabs](https://elevenlabs.io/) API key with realtime STT and WebSocket TTS access (`voices_read` and `user_read` enable the full sidebar experience)
- At least one model-provider key: [xAI](https://x.ai/), [DeepSeek](https://www.deepseek.com/), or [Google AI Studio](https://aistudio.google.com/)
- A browser with microphone access

### Run locally

```bash
git clone https://github.com/slayermm1122/os1.git
cd os1

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Add your provider keys to `.env`:

```dotenv
ELEVENLABS_API_KEY=your_key

# Choose one brain: xai, deepseek, or google.
LLM_PROVIDER=xai
XAI_API_KEY=your_key

# Optional: configure more brains for in-app switching.
# DEEPSEEK_API_KEY=your_key
# GEMINI_API_KEY=your_key
```

Start OS1 on the loopback interface:

```bash
.venv/bin/uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), allow microphone access, and press the orb. Provider and voice selections made in the sidebar are written back to the local `.env` file.

SQLite needs no separate installation or setup. Python includes the driver, and OS1 creates `data/telemetry.sqlite` plus its schema automatically on first start. The Usage view is initially empty and begins filling after completed turns; telemetry is enabled by default through `TELEMETRY_ENABLED=true`.

## What is inside

| Area | Current support |
| --- | --- |
| Brain | Grok `grok-4.5`, DeepSeek `deepseek-v4-flash`, Gemini `gemini-3.5-flash-lite` |
| Listening | ElevenLabs Scribe realtime STT, VAD turn detection, keyterms, English and Chinese detection |
| Speaking | ElevenLabs streaming TTS, My Voices, previews, pronunciation aliases, timestamp alignment |
| Conversation | Persistent live session, per-session context, barge-in, synchronized captions |
| Personalization | Assistant and user names, three response styles, language selection, eight motion styles |
| Observability | Per-provider latency, token, audio, character, cache, status, and error metrics in an auto-created local SQLite database |

## How it works

![How OS1 turns speech into a live voice response](https://cdn.jsdelivr.net/gh/slayermm1122/os1@dd887547917f109d9877d37298281f88beb11045/docs/assets/os1-flow.svg)

One browser-to-backend WebSocket carries the live session. Scribe remains connected across turns, the selected language model streams its response, and a preconnected multi-context TTS socket begins playback before the full answer is complete.

The backend is intentionally split at capability boundaries, so model, speech-to-text, and text-to-speech providers are isolated behind gateway interfaces rather than coupled to the UI.

```text
backend/
  api/          HTTP and realtime WebSocket transport
  core/         orchestration, sessions, settings, and safety boundaries
  gateways/     model, STT, TTS, voice, and account integrations
  telemetry/    local SQLite metrics

frontend/
  index.html    the single-window voice interface
  captions.js   audio-clock synchronized captions
```

## Local data and security

OS1 is designed to run only on `127.0.0.1`; it is not a public web service. API keys and core settings stay in `.env`; the selected motion style is the only preference stored in browser local storage. New telemetry records contain operational metrics rather than transcripts, prompts, or replies. Conversation context is kept in memory for the local session and is cleared when the backend restarts.

Read [SECURITY.md](SECURITY.md) before changing the network boundary or deploying OS1 anywhere beyond your own machine. Existing telemetry databases created by older versions may still contain historical conversation content.

## Development

Run the complete test suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Check a running instance:

```bash
curl http://127.0.0.1:8000/api/health
```

Release history is kept in [CHANGELOG.md](CHANGELOG.md).

## Scope

OS1 is personal, local-first research software. It currently depends on external model and speech APIs and is not intended for public or multi-user deployment.

Its visual atmosphere is inspired by the warmth and intimacy associated with Samantha's OS in *Her*, not by the film's literal interface. This project is independent and is not affiliated with or endorsed by the film, its studios, or its rights holders.
