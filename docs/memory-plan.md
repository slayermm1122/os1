# OS1 Memory Plan

Status: planned for `v0.05`.

## Product Direction

OS1 is intended to remain one continuous AI surface. It will not introduce a sidebar of named conversations or make users decide which chat contains a piece of context.

Continuity therefore has to come from memory maintenance, not from exposing a collection of transcript containers. The future memory layer must decide what is worth retaining, how it changes over time, and when it should influence an answer. Replaying an unlimited message history is explicitly not the target architecture.

## Current Implementation

The current system has two separate forms of history:

1. Runtime conversation context is held by `SessionStore` in process memory. It keeps the latest eight complete user/assistant turns by default, expires an idle session after one hour, and is cleared whenever the backend restarts.
2. Optional telemetry persists operational records in `data/telemetry.sqlite`. A `turn` is the highest-level persisted entity. Each STT, knowledge, LLM, TTS, event, and error record belongs to a `turn_id`.

The telemetry `turns` table contains a `session_id` label for correlation, but there is no `sessions`, `conversations`, or `memories` table and no persisted parent object above a turn. Telemetry is diagnostic evidence, not application memory or business state.

The prompt currently contains:

```text
system instruction
optional knowledge-search results
recent in-memory message history
current user message
```

## Invariants For v0.05

- Preserve the single-window interaction model.
- Do not equate memory with storing or replaying every transcript.
- Keep immutable turn records separate from derived memory.
- Make every memory item traceable to the turn or evidence that created it.
- Support correction and forgetting; retained information cannot be append-only truth.
- Keep memory retrieval separate from handbook and document retrieval.
- Keep provider gateways independent from memory policy.
- Measure the latency, token cost, and hit quality introduced by memory operations.
- Treat stored memory as private user data with an explicit deletion lifecycle.

## Design Work Intentionally Deferred

The dedicated memory design phase will decide:

- Memory types and their boundaries, such as preferences, durable facts, commitments, and short-lived context.
- Extraction and consolidation timing.
- Confidence, provenance, contradiction, correction, expiry, and forgetting rules.
- Retrieval and ranking without treating memory as generic document RAG.
- The relationship between one continuous identity and temporary task context.
- Database schema above or beside `turn_id`.
- User visibility and controls that preserve the no-conversation-list product idea.

No schema in this document should be treated as decided before that design work.
