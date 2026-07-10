from __future__ import annotations

import re
import sqlite3
from contextlib import closing

from ...config import Settings
from ...core.storage import prepare_private_directory, secure_private_files, sqlite_files
from .base import SearchHit


SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt"}


class SQLiteFTSKnowledgeGateway:
    provider = "sqlite_fts5"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path = settings.knowledge_db_path
        self.docs_dir = settings.knowledge_docs_dir
        self.manage_db_parent = (
            self.db_path.parent.resolve() == (settings.root_dir / "data").resolve()
        )
        self.manage_docs_dir = (
            self.docs_dir.resolve() == (settings.root_dir / "knowledge_docs").resolve()
        )

    @property
    def enabled(self) -> bool:
        return self.settings.knowledge_enabled

    def ensure_index(self) -> None:
        prepare_private_directory(
            self.db_path.parent,
            manage_existing=self.manage_db_parent,
        )
        prepare_private_directory(
            self.docs_dir,
            manage_existing=self.manage_docs_dir,
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts "
                "USING fts5(path, chunk, content, tokenize='unicode61')"
            )
            conn.commit()
            secure_private_files(sqlite_files(self.db_path))

    def reindex(self) -> int:
        self.ensure_index()
        files = [
            path
            for path in self.docs_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        with closing(sqlite3.connect(self.db_path)) as conn:
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
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT path, chunk, snippet(docs_fts, 2, '[', ']', '...', 32)
                FROM docs_fts
                WHERE docs_fts MATCH ?
                ORDER BY bm25(docs_fts)
                LIMIT ?
                """,
                (match_query, limit or self.settings.knowledge_limit),
            ).fetchall()
        return [SearchHit(path=row[0], chunk=row[1], snippet=row[2]) for row in rows]

    def format_hits(self, hits: list[SearchHit]) -> str:
        return "\n\n".join(
            f"[{index}] {hit.path}#chunk-{hit.chunk}\n{hit.snippet}"
            for index, hit in enumerate(hits, start=1)
        )


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
