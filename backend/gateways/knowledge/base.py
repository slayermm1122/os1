from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SearchHit:
    path: str
    chunk: str
    snippet: str


class KnowledgeGateway(Protocol):
    provider: str

    @property
    def enabled(self) -> bool: ...

    def ensure_index(self) -> None: ...

    def reindex(self) -> int: ...

    def search(self, query: str, limit: int | None = None) -> list[SearchHit]: ...

    def format_hits(self, hits: list[SearchHit]) -> str: ...
