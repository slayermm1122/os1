# OS1 Changelog

This file records the user-visible and internal changes in each OS1 release. Versions follow the project's `X.Y.Z` convention described in the README.

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
