<div align="center">
  <img src="https://cdn.jsdelivr.net/gh/slayermm1122/os1@ac93844/docs/assets/os1-logo.png" width="88" alt="OS1" />
  <h1>
    OS1<br />
    <sub><sub>a fully open sourced AI coworker and companion</sub></sub>
  </h1>
  <p>
    <a href="#quick-start">Quick start</a>
    &nbsp;·&nbsp;
    <a href="#how-it-works">How it works</a>
    &nbsp;·&nbsp;
    <a href="#roadmap">Roadmap</a>
    &nbsp;·&nbsp;
    <a href="SECURITY.md">Security</a>
  </p>
</div>

<br />

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/slayermm1122/os1@4a681937965e1e941a65e26b46305b6a6d87f294/docs/assets/os1-home.jpg" width="100%" alt="OS1 listening with the Plasma orb" />
</p>

<p align="center"><sub>Powered by ElevenLabs, Grok, DeepSeek and all major labs.</sub></p>

## One room. One voice. One continuous conversation.

**Less interface.** No thread picker. No maze of chat windows. OS1 is one continuous surface where your AI can move between helping with a task and simply being present. As memory and task intelligence grow, the boundary between coworker and companion should disappear—so you spend less time finding the right window and more time moving forward.

**Talk naturally.** Tap once and start speaking. OS1 knows when you finish, begins answering before the full response is ready, and lets you interrupt as naturally as you would another person. Use automatic language detection or switch the response language yourself.

**Make it yours.** Name your AI, tell it your name, choose its conversational style, and select the voice you want to hear. Connect it to language models from frontier AI labs and shape how it listens, speaks, and appears.

**Voice only.** Speech is the most instinctive interface we have. OS1 is intentionally built around listening and speaking, without a text box or typed-chat mode. The goal is not to make another messenger—it is to make interacting with AI feel immediate, embodied, and human.

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/slayermm1122/os1@dd887547917f109d9877d37298281f88beb11045/docs/assets/os1-motion.jpg" width="100%" alt="Choosing an OS1 orb motion" />
</p>

<p align="center"><sub>Personalize the responsive motion of your AI.</sub></p>

## Quick start

You need Python 3.11+, a browser with microphone access, an ElevenLabs API key, and a key for at least one supported language-model provider.

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

# Choose xai, deepseek, or google.
LLM_PROVIDER=xai
XAI_API_KEY=your_key

# Optional: add more brains for in-app switching.
# DEEPSEEK_API_KEY=your_key
# GEMINI_API_KEY=your_key
```

Start the room:

```bash
.venv/bin/uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), allow microphone access, and press the orb.

SQLite needs no separate installation or setup. OS1 creates `data/telemetry.sqlite` and its schema on first start. Usage begins filling after your first completed turns.

<details>
<summary><strong>Current support</strong></summary>

| | |
| --- | --- |
| Brain | Grok `grok-4.5`, DeepSeek `deepseek-v4-flash`, Gemini `gemini-3.5-flash-lite` |
| Listening | ElevenLabs Scribe realtime STT, VAD turn detection, keyterms, English and Chinese detection |
| Speaking | ElevenLabs Flash v2.5 streaming TTS for a faster, more natural realtime experience; My Voices, previews, pronunciation aliases, and timestamp alignment |
| Conversation | Persistent live session, per-session context, barge-in, synchronized captions |
| Personalization | AI and user names, three response styles, language selection, eight motion styles |
| Observability | Provider latency, usage, cache, status, and error metrics in local SQLite |

The ElevenLabs key needs realtime STT and WebSocket TTS access. `voices_read` and `user_read` enable the complete voice-library and account experience.

</details>

## How it works

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/slayermm1122/os1@dd887547917f109d9877d37298281f88beb11045/docs/assets/os1-flow.svg" width="100%" alt="How OS1 turns speech into a live voice response" />
</p>

One browser-to-backend WebSocket carries the live session. Scribe stays connected across turns, the selected language model streams its response, and a preconnected multi-context TTS socket begins playback before the answer is complete.

### Bring your own brain

Add a provider key to `.env`, then choose the model from **OS1 → Brain**. OS1 currently supports xAI, DeepSeek, and Google AI Studio. A selection applies to the next response, so you can move between configured models without restarting the conversation.

### Bring your own voice

Connect your ElevenLabs account with `ELEVENLABS_API_KEY`, then open **OS1 → Voice**. OS1 loads the voices in your ElevenLabs library automatically, lets you preview and select them, and shows the account name and remaining credits. You can also choose automatic, English, or Chinese responses and tune when silence ends your turn.

### Understand your usage

Open **OS1 → Usage** to see the last seven days, thirty days, or all local history. OS1 separates language-model usage from ElevenLabs usage and shows calls, latency, tokens and cache behavior, audio duration, TTS characters, model breakdowns, daily activity, failures, and cancellations. These metrics come from the local SQLite database; new records do not contain conversation text.

<details>
<summary><strong>Project structure</strong></summary>

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

</details>

## Local by design

OS1 is designed for `127.0.0.1`, not public or multi-user deployment. API keys and core settings stay in `.env`; the selected motion style is the only preference stored in browser local storage. New telemetry contains operational metrics, not transcripts, prompts, or replies.

Read [SECURITY.md](SECURITY.md) before changing the network boundary.

## Roadmap

OS1 will keep the single-room experience while becoming more capable underneath it.

- **More model APIs.** Connect more frontier model providers without changing how the conversation feels.
- **Local models.** Run compatible language and speech models on your own hardware when privacy, control, or offline use matters most.
- **A deeper memory system.** Build deliberate, correctable memory that separates durable knowledge, active tasks, preferences, and short-lived context—without turning OS1 into a list of chats.

Have an idea for what your AI should become? [Open an issue](https://github.com/slayermm1122/os1/issues) and tell us. We will do our best to make the most thoughtful ideas real.

<details>
<summary><strong>Development</strong></summary>

```bash
# Run the test suite.
.venv/bin/python -m unittest discover -s tests -v

# Check a running instance.
curl http://127.0.0.1:8000/api/health
```

Release history is kept in [CHANGELOG.md](CHANGELOG.md).

</details>

---

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/slayermm1122/os1@ac93844/docs/assets/os1-logo.png" width="54" alt="OS1" />
</p>

<p align="center">
  <sub>
    OS1 — a personal AI that is truly yours.<br />
    Inspired by the warmth of Samantha's OS in <em>Her</em>, not by the film's literal interface.<br />
    Independent and not affiliated with or endorsed by the film, its studios, or its rights holders.
  </sub>
</p>

<p align="center"><sub><strong>For research and educational use only.</strong> This experimental software is provided as-is, without warranty. You are responsible for how you configure and use it.</sub></p>
