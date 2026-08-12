# Changelog

The notable changes in each OS1 release. Unreleased work reflects the current `main` branch.

## Unreleased

- Added one continuous Live voice session with automatic VAD turns and natural barge-in.
- Added Grok, DeepSeek, and Gemini selection with provider-specific low-latency settings.
- Added true streaming ElevenLabs Flash v2.5 speech on a reusable multi-context connection.
- Added Persona, Voice, Motion, Pronunciation, Usage, and API-key status views.
- Added My Voices, previews, English/Chinese response modes, listening controls, and pronunciation rules.
- Added eight responsive orb motions and a quieter single-window interface.
- Added content-free local usage metrics by model and date range.

## v0.03.03 — 2026-07-25

- Replaced timed recording with automatic end-of-speech detection.
- Added listening sensitivity, silence timing, persona names, and response styles.
- Added chat bubbles, mic-reactive motion, and shorter TTS-friendly replies.

## v0.03.02 — 2026-07-13

- Synchronized live captions with STT word timing and the browser audio clock.
- Added TTS character alignment and a graceful fallback when timing is unavailable.

## v0.03.01 — 2026-07-13

- Added an experimental local knowledge context and its retrieval verification workflow.
- The knowledge feature was later retired so OS1 could return to a focused voice surface.

## v0.02.03 — 2026-07-11

- Made readiness checks compatible with restricted ElevenLabs keys.
- Added clearer, sanitized provider errors for authentication, credits, rate limits, and transport failures.

## v0.02.02 — 2026-07-11

- Added provider readiness checks before voice conversation begins.
- Added clear connection states, retry behavior, and a quiet thinking animation.

## v0.02.01 — 2026-07-10

- Split transport, orchestration, provider gateways, sessions, errors, and telemetry into independent modules.
- Added structured local telemetry, request correlation, safety limits, and automated backend tests.

## v0.02 — 2026-07-10

- Introduced the realtime STT → language model → streaming TTS pipeline.
- Added browser PCM streaming, AudioContext playback, and initial voice selection.

## v0.01 — 2026-07-09

- Released the first single-window OS1 prototype.
- Connected microphone input, Grok responses, and ElevenLabs speech in the warm voice interface.
