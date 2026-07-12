from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ...core.messages import Message


@dataclass(frozen=True)
class SearchHit:
    path: str
    chunk: str
    snippet: str


@dataclass(frozen=True)
class KnowledgeEvidence:
    evidence_id: str
    provider: str
    kind: str
    title: str
    content: str
    source_file: str | None = None
    doc_id: str | None = None
    chunk_id: str | None = None
    wiki_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    score: float | None = None

    @property
    def path(self) -> str:
        return self.source_file or self.wiki_id or self.evidence_id

    @property
    def chunk(self) -> str:
        return self.chunk_id or self.wiki_id or self.evidence_id

    @property
    def snippet(self) -> str:
        return self.content[:320]


class KnowledgeGateway(Protocol):
    provider: str

    @property
    def enabled(self) -> bool: ...

    def ensure_index(self) -> None: ...

    def reindex(self) -> int: ...

    def search(self, query: str, limit: int | None = None) -> list[SearchHit]: ...

    def format_hits(self, hits: list[SearchHit]) -> str: ...


class KnowledgeSearchProvider(Protocol):
    provider: str

    async def search(
        self,
        query: str,
        *,
        history: list[Message],
        api_key: str | None,
        cache_key: str,
        trace: object | None = None,
    ) -> list[KnowledgeEvidence]: ...
