from __future__ import annotations

import asyncio
import time
import uuid

from ...core.messages import Message
from ...telemetry.sqlite_store import SQLiteTelemetryRecorder, TurnTrace, utc_now
from ..llm import AIGateway, LLMRequest
from .base import KnowledgeEvidence
from .sqlite_fts import SQLiteFTSKnowledgeGateway
from .wiki import WikiCatalog


WIKI_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "wiki_ids": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 2,
        }
    },
    "required": ["wiki_ids"],
    "additionalProperties": False,
}


class LexSearch:
    provider = "lex_search"

    def __init__(self, index: SQLiteFTSKnowledgeGateway) -> None:
        self.index = index

    async def search(
        self,
        query: str,
        *,
        history: list[Message],
        api_key: str | None,
        cache_key: str,
        trace: object | None = None,
    ) -> list[KnowledgeEvidence]:
        del history, api_key, cache_key, trace
        return await asyncio.to_thread(self.index.search_evidence, query)


class LLMSearch:
    provider = "llm_search.xai"

    def __init__(
        self,
        ai: AIGateway,
        catalog: WikiCatalog,
        telemetry: SQLiteTelemetryRecorder,
        *,
        limit: int = 2,
    ) -> None:
        self.ai = ai
        self.catalog = catalog
        self.telemetry = telemetry
        self.limit = max(1, min(limit, 2))

    async def search(
        self,
        query: str,
        *,
        history: list[Message],
        api_key: str | None,
        cache_key: str,
        trace: object | None = None,
    ) -> list[KnowledgeEvidence]:
        catalog_text = self.catalog.prompt_catalog()
        if not catalog_text:
            return []
        recent = history[-2:]
        conversation_hint = "\n".join(
            f"{message['role']}: {message['content'][:500]}" for message in recent
        )
        request = LLMRequest(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Select at most two wiki pages that are directly useful for answering the "
                        "English user question. Return an empty list when none apply.\n\n"
                        f"Wiki catalog:\n{catalog_text}"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Recent conversation:\n{conversation_hint or '(none)'}\n\n"
                        f"Current question:\n{query}"
                    ),
                },
            ],
            api_key=api_key,
            cache_key=f"{cache_key}:wiki:{self.catalog.version()}",
            purpose="knowledge_selector",
        )
        call_id = uuid.uuid4().hex
        started = time.perf_counter()
        observed_trace = trace if isinstance(trace, TurnTrace) else None
        if observed_trace:
            self.telemetry.start_llm_call(
                observed_trace, call_id=call_id, provider=self.ai.provider, model=self.ai.model,
                reasoning_effort=self.ai.reasoning_effort, purpose=request.purpose,
                request={"model": self.ai.model, "messages": request.messages, "stream": False},
            )
        try:
            result = await self.ai.generate_object(
                request, schema_name="wiki_selection", schema=WIKI_SELECTION_SCHEMA
            )
        except asyncio.CancelledError:
            if observed_trace:
                self.telemetry.finish_llm_call(
                    call_id, status="cancelled", completed_at=utc_now(),
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
            raise
        except Exception:
            if observed_trace:
                self.telemetry.finish_llm_call(
                    call_id, status="failed", completed_at=utc_now(),
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
            raise

        raw_ids = result.value.get("wiki_ids")
        if not isinstance(raw_ids, list):
            raw_ids = []
        valid_ids = self.catalog.pages()
        selected: list[str] = []
        for value in raw_ids:
            wiki_id = str(value)
            if wiki_id in valid_ids and wiki_id not in selected:
                selected.append(wiki_id)
            if len(selected) >= self.limit:
                break
        if observed_trace:
            usage = result.usage
            self.telemetry.finish_llm_call(
                call_id, status="success", completed_at=utc_now(),
                duration_ms=(time.perf_counter() - started) * 1000,
                response_text=result.raw_text,
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
                cached_tokens=usage.cached_tokens if usage else None,
                reasoning_tokens=usage.reasoning_tokens if usage else None,
                cache_status=usage.cache_status if usage else None,
                cost_usd_ticks=usage.cost_usd_ticks if usage else None,
                finish_reason=result.finish_reason, provider_request_id=result.request_id,
                system_fingerprint=result.system_fingerprint, service_tier=result.service_tier,
            )
        return self.catalog.load_evidence(selected, self.provider)
