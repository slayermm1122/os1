# OS1 Changelog

This file records the user-visible and internal changes in each OS1 release. Versions follow the project's `X.Y.Z` convention described in the README.

## v0.03.03 - 2026-07-25

Voice conversation polish: VAD listening, chat bubbles, quieter settings.

- Replaced the press-to-stop / 15-second recording limit with ElevenLabs realtime STT VAD end-of-speech.
- Added adjustable silence and sensitivity controls in the Voice settings panel.
- Redesigned Keys and Voice settings toward a calmer OS1 visual language.
- Moved male / female selection into Voice settings and restored quiet text toggles.
- Added an optional English assistant name injected into the system prompt.
- Drove recording orb bars from live microphone levels; kept assistant speaking motion small.
- Softened the orb hover glow so it fades into the room without a hard plate ring.
- Replaced centered transcript lines with center-panel chat bubbles (user right, assistant left).
- Tightened spoken-chat system instructions so replies stay short for live conversation and TTS.
- Hid advanced product surfaces behind configuration for a leaner default voice experience.

## v0.03.02 - 2026-07-13

Timestamp-synchronized live captions.

- Enabled ElevenLabs realtime STT word timing and prevented committed transcripts from being emitted twice.
- Requested ElevenLabs TTS character alignment and forwarded it alongside streamed PCM audio.
- Drove spoken-response captions from the browser audio output clock so highlighted text follows what the user actually hears.
- Preserved a graceful timing fallback when alignment metadata is unavailable.
- Added SOCKS proxy support and timing parser, protocol, and multi-chunk alignment coverage.

## v0.03.01 - 2026-07-13

Document knowledge and cache-aware conversation context.

- Added a dedicated read-only Knowledge Base page for inspecting wiki pages and canonical chunks; upload and rebuild controls remain disabled while ingestion is designed separately.
- Validated the knowledge pipeline with a hand-compiled local Attention Is All You Need corpus; all raw documents and derived knowledge remain git-ignored user data.
- Added parallel local lexical search and Grok 4.5 low-reasoning wiki selection with a configurable, fail-open deadline, currently 5 seconds.
- Added provider-neutral streaming-text and structured-object AI gateway operations with xAI cache routing.
- Added KV Conversation storage for the exact enriched user messages sent to the answer model.
- Moved dynamic knowledge context from the system message to the latest user message.
- Added deterministic corpus hashing, JSONL validation, atomic index replacement, weighted BM25, and per-purpose AI telemetry.
- Enabled local telemetry by default and added per-provider knowledge outcomes for normal hits, misses, timeouts, cancellations, and failures.
- Added an expandable, collapsible, and hideable Ref panel showing the exact wiki pages and chunks selected for each answer.
- Added a per-document retrieval golden set and search-only self-verification loop with Recall@5, precision, MRR, nDCG@5, and SQLite run history.

## v0.02.03 - 2026-07-11

Restricted-key readiness and provider error clarity.

- Replaced ElevenLabs model metadata checks with exact `realtime_scribe` and `tts_websocket` capability checks.
- Fixed valid restricted ElevenLabs keys being reported as invalid when `models_read` or `user_read` was disabled.
- Added sanitized provider error type, code, status, message, HTTP status, request ID, and retryability to frontend error events.
- Distinguished authentication, authorization, insufficient-credit, rate-limit, and transport failures in the interface.
- Verified the local key with minimal live TTS and realtime STT smoke tests.

## v0.02.02 - 2026-07-11

Provider readiness and response-state feedback.

- Added a no-generation startup check for the configured xAI model and ElevenLabs voice path.
- Kept the talk button disabled until both provider checks succeed.
- Added concise connection states and retry behavior for unavailable providers or rejected keys.
- Added a restrained thinking animation between committed transcription and streamed voice playback.
- Preserved the existing first-token and turn-event latency measurements.

## v0.02.01 - 2026-07-10

Backend modularization and observability release.

- Split HTTP and WebSocket transport, turn orchestration, provider gateways, session history, errors, and telemetry into independent modules.
- Added replaceable Protocol boundaries for LLM, STT, TTS, and knowledge providers.
- Added opt-in SQLite telemetry for turns, stage events, provider calls, latency, usage, cache behavior, cost metadata, and structured errors.
- Added canonical `turn_id` and per-provider `call_id` correlation across the voice pipeline.
- Added explicit success, failure, partial failure, and cancellation states.
- Added local-only network boundaries, safer browser key storage, request limits, security headers, credential redaction, and private local database permissions.
- Added backend unit and WebSocket integration tests using fake providers.

## v0.02 - 2026-07-10

Realtime voice experience.

- Streamed browser PCM to ElevenLabs realtime STT while the user was recording.
- Streamed Grok output into ElevenLabs TTS and played PCM chunks through AudioContext.
- Preserved the press-to-talk interaction with a 15-second recording limit.
- Added male and female voice selection and refined responsive layout, transcript placement, and the circular OS1 logo.

## v0.01 - 2026-07-09

Initial prototype.

- Added the single-window orange-red voice interface.
- Added click-to-record and stop-to-submit interaction.
- Connected ElevenLabs transcription and speech generation with xAI Grok responses.
- Added browser API-key setup, local `.env` fallback, project documentation, and the initial legal disclaimer.
