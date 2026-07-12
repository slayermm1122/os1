from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from ...config import Settings
from ...core.storage import prepare_private_directory, secure_private_files, sqlite_files
from .base import KnowledgeEvidence, SearchHit


INDEX_SCHEMA_VERSION = 1
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does", "for",
    "from", "get", "how", "in", "is", "it", "model", "of", "on", "or", "that", "the",
    "this", "to", "was", "were", "what", "when", "where", "which", "why", "with",
}
REQUIRED_FIELDS = {
    "chunk_id", "doc_id", "document_title", "section_title", "title_path",
    "text", "source_file", "page_start", "page_end", "chunk_order", "language",
}


class JSONLValidationError(ValueError):
    pass


class SQLiteFTSKnowledgeGateway:
    """A disposable FTS5 index built exclusively from canonical JSONL chunks."""

    provider = "lex_search.sqlite_fts5"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path = settings.knowledge_db_path
        self.root_dir = settings.knowledge_root_dir
        self.chunks_dir = self.root_dir / "chunks"
        self.docs_dir = settings.knowledge_docs_dir
        self.manage_db_parent = self.db_path.parent.resolve() == (settings.root_dir / "data").resolve()
        self.manage_root = self.root_dir.resolve() == (settings.root_dir / "knowledge").resolve()

    @property
    def enabled(self) -> bool:
        return self.settings.knowledge_enabled

    def ensure_index(self) -> None:
        self._prepare_paths()
        corpus_hash = self.corpus_hash()
        if self._is_current(corpus_hash):
            return
        self.reindex()

    def reindex(self) -> int:
        self._prepare_paths()
        rows = list(load_jsonl_chunks(self.chunks_dir))
        corpus_hash = self.corpus_hash()
        fd, temporary_name = tempfile.mkstemp(
            prefix="knowledge-", suffix=".sqlite", dir=self.db_path.parent
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            with closing(sqlite3.connect(temporary_path)) as conn:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute(
                    "CREATE TABLE index_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE VIRTUAL TABLE docs_fts USING fts5("
                    "chunk_id UNINDEXED, doc_id UNINDEXED, source_file UNINDEXED, "
                    "page_start UNINDEXED, page_end UNINDEXED, chunk_order UNINDEXED, "
                    "document_title, section_title, title_path, text, tokenize='unicode61')"
                )
                for row in rows:
                    conn.execute(
                        "INSERT INTO docs_fts(chunk_id, doc_id, source_file, page_start, page_end, "
                        "chunk_order, document_title, section_title, title_path, text) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            row["chunk_id"], row["doc_id"], row["source_file"],
                            row["page_start"], row["page_end"], row["chunk_order"],
                            row["document_title"], row["section_title"], row["title_path"], row["text"],
                        ),
                    )
                conn.executemany(
                    "INSERT INTO index_metadata(key, value) VALUES (?, ?)",
                    (("schema_version", str(INDEX_SCHEMA_VERSION)), ("corpus_hash", corpus_hash)),
                )
                conn.commit()
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.db_path)
            secure_private_files(sqlite_files(self.db_path))
        finally:
            temporary_path.unlink(missing_ok=True)
        return len(rows)

    def search_evidence(self, query: str, limit: int | None = None) -> list[KnowledgeEvidence]:
        if not self.enabled:
            return []
        self.ensure_index()
        match_query = _fts_query(query)
        if not match_query:
            return []
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT chunk_id, doc_id, source_file, page_start, page_end,
                       document_title, section_title, text,
                       bm25(docs_fts, 0, 0, 0, 0, 0, 0, 6.0, 5.0, 4.0, 1.0)
                FROM docs_fts
                WHERE docs_fts MATCH ?
                ORDER BY bm25(docs_fts, 0, 0, 0, 0, 0, 0, 6.0, 5.0, 4.0, 1.0)
                LIMIT ?
                """,
                (match_query, limit or self.settings.knowledge_limit),
            ).fetchall()
        return [
            KnowledgeEvidence(
                evidence_id=f"chunk:{row[0]}", provider=self.provider, kind="raw_chunk",
                title=" — ".join(part for part in (row[5], row[6]) if part), content=row[7],
                source_file=row[2], doc_id=row[1], chunk_id=row[0],
                page_start=_integer(row[3]), page_end=_integer(row[4]), score=float(row[8]),
            )
            for row in rows
        ]

    def search(self, query: str, limit: int | None = None) -> list[SearchHit]:
        return [SearchHit(path=item.path, chunk=item.chunk, snippet=item.content) for item in self.search_evidence(query, limit)]

    def format_hits(self, hits: list[SearchHit] | list[KnowledgeEvidence]) -> str:
        return format_evidence(hits, self.settings.knowledge_context_max_chars)

    def corpus_hash(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.chunks_dir.glob("*.jsonl")):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _prepare_paths(self) -> None:
        prepare_private_directory(self.db_path.parent, manage_existing=self.manage_db_parent)
        prepare_private_directory(self.root_dir, manage_existing=self.manage_root)
        manage_docs = self.docs_dir.resolve() in {
            (self.settings.root_dir / "knowledge" / "raw").resolve(),
            (self.settings.root_dir / "knowledge_docs").resolve(),
        }
        prepare_private_directory(self.docs_dir, manage_existing=manage_docs)
        prepare_private_directory(self.chunks_dir, manage_existing=self.manage_root)

    def _is_current(self, corpus_hash: str) -> bool:
        if not self.db_path.exists():
            return False
        try:
            with closing(sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)) as conn:
                values = dict(conn.execute("SELECT key, value FROM index_metadata").fetchall())
            return values == {
                "schema_version": str(INDEX_SCHEMA_VERSION),
                "corpus_hash": corpus_hash,
            }
        except (sqlite3.Error, ValueError):
            return False


def load_jsonl_chunks(chunks_dir: Path):
    seen: set[str] = set()
    for path in sorted(chunks_dir.glob("*.jsonl")):
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise JSONLValidationError(f"{path.name}:{line_number}: invalid JSON") from exc
            if not isinstance(row, dict):
                raise JSONLValidationError(f"{path.name}:{line_number}: expected an object")
            missing = REQUIRED_FIELDS - set(row)
            if missing:
                raise JSONLValidationError(
                    f"{path.name}:{line_number}: missing {', '.join(sorted(missing))}"
                )
            chunk_id = str(row["chunk_id"]).strip()
            doc_id = str(row["doc_id"]).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", chunk_id) or chunk_id in seen:
                raise JSONLValidationError(f"{path.name}:{line_number}: duplicate or empty chunk_id")
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", doc_id):
                raise JSONLValidationError(f"{path.name}:{line_number}: invalid doc_id")
            for field in ("document_title", "section_title", "title_path", "source_file"):
                if not isinstance(row[field], str) or not row[field].strip():
                    raise JSONLValidationError(f"{path.name}:{line_number}: invalid {field}")
            source_path = Path(str(row["source_file"]))
            if source_path.is_absolute() or ".." in source_path.parts:
                raise JSONLValidationError(f"{path.name}:{line_number}: unsafe source_file")
            if str(row["language"]).lower() != "en":
                raise JSONLValidationError(
                    f"{path.name}:{line_number}: v0.03.01 only supports English"
                )
            page_start, page_end = _integer(row["page_start"]), _integer(row["page_end"])
            if page_start is None or page_end is None or page_start < 1 or page_end < page_start:
                raise JSONLValidationError(f"{path.name}:{line_number}: invalid page range")
            if not str(row["text"]).strip():
                raise JSONLValidationError(f"{path.name}:{line_number}: empty text")
            chunk_order = _integer(row["chunk_order"])
            if chunk_order is None or chunk_order < 0:
                raise JSONLValidationError(f"{path.name}:{line_number}: invalid chunk_order")
            seen.add(chunk_id)
            yield row


def format_evidence(
    hits: list[SearchHit] | list[KnowledgeEvidence], max_chars: int
) -> str:
    sections: list[str] = []
    used = 0
    for index, hit in enumerate(hits, 1):
        if isinstance(hit, KnowledgeEvidence):
            location = hit.source_file or hit.wiki_id or hit.evidence_id
            if hit.page_start:
                location += f" p.{hit.page_start}" + (f"-{hit.page_end}" if hit.page_end != hit.page_start else "")
            body = hit.content
            title = hit.title
        else:
            location, body, title = f"{hit.path}#{hit.chunk}", hit.snippet, hit.path
        section = f"[Reference {index}] {title}\nSource: {location}\n{body.strip()}"
        if max_chars > 0 and used + len(section) > max_chars:
            break
        sections.append(section)
        used += len(section)
    return "\n\n".join(sections)


def _fts_query(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", query.lower())
    deduped: list[str] = []
    for term in terms:
        if len(term) < 2 or term in STOP_WORDS or term in deduped:
            continue
        deduped.append(term[:64])
        if len(deduped) >= 12:
            break
    return " OR ".join(f'"{term}"' for term in deduped)


def _integer(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
