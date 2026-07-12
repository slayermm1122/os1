from __future__ import annotations

from pathlib import Path

from .sqlite_fts import load_jsonl_chunks
from .wiki import WikiCatalog


class KnowledgeBrowser:
    """Read-only management view over canonical wiki and chunk artifacts."""

    def __init__(self, knowledge_root: Path) -> None:
        self.knowledge_root = knowledge_root
        self.catalog = WikiCatalog(knowledge_root / "wiki")
        self.chunks_dir = knowledge_root / "chunks"

    def overview(self) -> dict[str, object]:
        pages = self.catalog.pages()
        chunks = list(load_jsonl_chunks(self.chunks_dir))
        return {
            "wiki": [
                {"id": page.wiki_id, "title": page.title, "summary": page.summary}
                for page in pages.values()
            ],
            "chunks": [
                {
                    "id": str(row["chunk_id"]),
                    "document_title": str(row["document_title"]),
                    "section_title": str(row["section_title"]),
                    "page_start": int(row["page_start"]),
                    "page_end": int(row["page_end"]),
                }
                for row in chunks
            ],
        }

    def wiki_page(self, wiki_id: str) -> dict[str, object] | None:
        page = self.catalog.pages().get(wiki_id)
        if page is None:
            return None
        return {
            "id": page.wiki_id,
            "title": page.title,
            "summary": page.summary,
            "content": page.content,
            "source_file": str(page.path.relative_to(self.knowledge_root)),
        }

    def chunk(self, chunk_id: str) -> dict[str, object] | None:
        for row in load_jsonl_chunks(self.chunks_dir):
            if str(row["chunk_id"]) == chunk_id:
                return dict(row)
        return None
