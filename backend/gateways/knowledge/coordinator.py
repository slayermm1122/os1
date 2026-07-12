from __future__ import annotations

import asyncio
import logging
import time
import uuid

from ...config import Settings
from ...core.errors import error_info
from ...core.messages import Message
from ...telemetry.sqlite_store import SQLiteTelemetryRecorder, TurnTrace, utc_now
from .base import KnowledgeEvidence, KnowledgeSearchProvider, SearchHit
from .sqlite_fts import SQLiteFTSKnowledgeGateway, format_evidence


logger = logging.getLogger("os1.knowledge")


class KnowledgeSearchCoordinator:
    provider = "knowledge.hybrid"

    def __init__(
        self,
        settings: Settings,
        index: SQLiteFTSKnowledgeGateway,
        providers: list[KnowledgeSearchProvider],
        *,
        telemetry: SQLiteTelemetryRecorder | None = None,
    ) -> None:
        self.settings = settings
        self.index = index
        self.providers = providers
        self.telemetry = telemetry

    @property
    def enabled(self) -> bool:
        return self.settings.knowledge_enabled

    def ensure_index(self) -> None:
        self.index.ensure_index()

    def reindex(self) -> int:
        return self.index.reindex()

    async def search_evidence(
        self,
        query: str,
        *,
        history: list[Message],
        api_key: str | None,
        cache_key: str,
        trace: object | None = None,
    ) -> list[KnowledgeEvidence]:
        if not self.enabled:
            return []
        tasks = [asyncio.create_task(
            self._search_provider(
                provider,
                query,
                history=history,
                api_key=api_key,
                cache_key=cache_key,
                trace=trace,
            ),
            name=f"knowledge-{provider.provider}",
        ) for provider in self.providers]
        if not tasks:
            return []
        provider_results = await asyncio.gather(*tasks, return_exceptions=True)
        merged: list[KnowledgeEvidence] = []
        seen: set[str] = set()
        for results in provider_results:
            if isinstance(results, BaseException):
                continue
            for evidence in results:
                if evidence.evidence_id in seen:
                    continue
                candidate_chars = sum(len(item.content) for item in merged) + len(evidence.content)
                if self.settings.knowledge_context_max_chars > 0 and candidate_chars > self.settings.knowledge_context_max_chars:
                    continue
                seen.add(evidence.evidence_id)
                merged.append(evidence)
        return merged

    async def _search_provider(
        self,
        provider: KnowledgeSearchProvider,
        query: str,
        *,
        history: list[Message],
        api_key: str | None,
        cache_key: str,
        trace: object | None,
    ) -> list[KnowledgeEvidence]:
        call_id = uuid.uuid4().hex
        started = time.perf_counter()
        observed_trace = trace if isinstance(trace, TurnTrace) else None
        if observed_trace and self.telemetry:
            self.telemetry.start_knowledge_call(
                observed_trace,
                call_id=call_id,
                provider=provider.provider,
                enabled=True,
                query_text=query,
            )
        try:
            results = await asyncio.wait_for(
                provider.search(
                    query,
                    history=history,
                    api_key=api_key,
                    cache_key=cache_key,
                    trace=trace,
                ),
                timeout=self.settings.knowledge_search_timeout_seconds,
            )
        except TimeoutError:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "Knowledge provider %s timed out after %.1f ms",
                provider.provider,
                duration_ms,
            )
            self._finish_provider_call(
                observed_trace,
                call_id,
                provider.provider,
                status="timeout",
                outcome="timeout",
                duration_ms=duration_ms,
            )
            return []
        except asyncio.CancelledError:
            self._finish_provider_call(
                observed_trace,
                call_id,
                provider.provider,
                status="cancelled",
                outcome="cancelled",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.warning("Optional knowledge provider %s failed: %s", provider.provider, exc)
            info = error_info(exc, default_stage="knowledge")
            if observed_trace and self.telemetry:
                self.telemetry.record_error(
                    observed_trace,
                    info,
                    call_id=call_id,
                    affect_turn=False,
                )
            self._finish_provider_call(
                observed_trace,
                call_id,
                provider.provider,
                status="failed",
                outcome="error",
                duration_ms=duration_ms,
                error_id=info.error_id,
            )
            return []

        outcome = "hit" if results else "miss"
        self._finish_provider_call(
            observed_trace,
            call_id,
            provider.provider,
            status="success",
            outcome=outcome,
            duration_ms=(time.perf_counter() - started) * 1000,
            results=results,
        )
        return results

    def _finish_provider_call(
        self,
        trace: TurnTrace | None,
        call_id: str,
        provider: str,
        *,
        status: str,
        outcome: str,
        duration_ms: float,
        results: list[KnowledgeEvidence] | None = None,
        error_id: str | None = None,
    ) -> None:
        if trace is None or self.telemetry is None:
            return
        evidence = results or []
        serialized = [
            {
                "evidence_id": item.evidence_id,
                "kind": item.kind,
                "title": item.title,
                "source_file": item.source_file,
                "chunk_id": item.chunk_id,
                "wiki_id": item.wiki_id,
                "score": item.score,
            }
            for item in evidence
        ]
        self.telemetry.finish_knowledge_call(
            call_id,
            status=status,
            completed_at=utc_now(),
            duration_ms=duration_ms,
            outcome=outcome,
            hit_count=len(evidence),
            results_json=serialized,
            error_id=error_id,
        )
        trace.event(
            "knowledge.provider.completed",
            stage="knowledge",
            metadata={
                "call_id": call_id,
                "provider": provider,
                "status": status,
                "outcome": outcome,
                "hit_count": len(evidence),
            },
        )

    def format_hits(self, hits: list[KnowledgeEvidence] | list[SearchHit]) -> str:
        return format_evidence(hits, self.settings.knowledge_context_max_chars)
