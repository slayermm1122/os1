from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import Settings


SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt"}


@dataclass(frozen=True)
class SearchHit:
    path: str
    chunk: str
    snippet: str


class KnowledgeBase:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path = settings.knowledge_db_path
        self.docs_dir = settings.knowledge_docs_dir

    @property
    def enabled(self) -> bool:
        return self.settings.knowledge_enabled

    def ensure_index(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts "
                "USING fts5(path, chunk, content, tokenize='unicode61')"
            )
            conn.commit()

    def reindex(self) -> int:
        self.ensure_index()
        files = [
            path
            for path in self.docs_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ]

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM docs_fts")
            indexed_chunks = 0
            for file_path in files:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
                relative_path = str(file_path.relative_to(self.docs_dir))
                for index, chunk in enumerate(_chunk_text(text)):
                    conn.execute(
                        "INSERT INTO docs_fts(path, chunk, content) VALUES (?, ?, ?)",
                        (relative_path, str(index), chunk),
                    )
                    indexed_chunks += 1
            conn.commit()
            return indexed_chunks

    def search(self, query: str, limit: int | None = None) -> list[SearchHit]:
        if not self.enabled:
            return []
        self.ensure_index()
        match_query = _fts_query(query)
        if not match_query:
            return []

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT
                    path,
                    chunk,
                    snippet(docs_fts, 2, '[', ']', '...', 32) AS snippet
                FROM docs_fts
                WHERE docs_fts MATCH ?
                ORDER BY bm25(docs_fts)
                LIMIT ?
                """,
                (match_query, limit or self.settings.knowledge_limit),
            ).fetchall()

        return [SearchHit(path=row[0], chunk=row[1], snippet=row[2]) for row in rows]

    def format_hits(self, hits: list[SearchHit]) -> str:
        lines: list[str] = []
        for idx, hit in enumerate(hits, start=1):
            lines.append(f"[{idx}] {hit.path}#chunk-{hit.chunk}\n{hit.snippet}")
        return "\n\n".join(lines)


def _chunk_text(text: str, size: int = 1400, overlap: int = 160) -> list[str]:
    normalized = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not normalized:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + size, len(normalized))
        boundary = normalized.rfind("\n\n", start, end)
        if boundary > start + size // 2:
            end = boundary
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == len(normalized):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _fts_query(query: str) -> str:
    terms = re.findall(r"[\w\u4e00-\u9fff]+", query.lower())
    deduped: list[str] = []
    for term in terms:
        if len(term) < 2 or term in deduped:
            continue
        deduped.append(term[:64])
        if len(deduped) >= 8:
            break
    return " OR ".join(f'"{term}"' for term in deduped)
