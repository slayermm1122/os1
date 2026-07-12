from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .base import KnowledgeEvidence


@dataclass(frozen=True)
class WikiPage:
    wiki_id: str
    title: str
    summary: str
    content: str
    path: Path


class WikiCatalog:
    def __init__(self, wiki_dir: Path) -> None:
        self.wiki_dir = wiki_dir
        self.pages_dir = wiki_dir / "pages"

    def pages(self) -> dict[str, WikiPage]:
        result: dict[str, WikiPage] = {}
        for path in sorted(self.pages_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            metadata, body = _frontmatter(text)
            wiki_id = metadata.get("id", "").strip()
            title = metadata.get("title", "").strip()
            summary = metadata.get("summary", "").strip()
            if not wiki_id or not title or not summary or wiki_id in result:
                continue
            result[wiki_id] = WikiPage(wiki_id, title, summary, body.strip(), path)
        return result

    def prompt_catalog(self) -> str:
        pages = self.pages()
        index_path = self.wiki_dir / "index.md"
        if not index_path.exists():
            return ""
        entries: list[str] = []
        for line in index_path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^- `([^`]+)`\s+[—-]\s+(.+)$", line.strip())
            if not match:
                continue
            wiki_id, summary = match.groups()
            page = pages.get(wiki_id)
            if page:
                entries.append(f"- {wiki_id}: {page.title} — {summary.strip()}")
        return "\n".join(entries)

    def version(self) -> str:
        return hashlib.sha256(self.prompt_catalog().encode()).hexdigest()[:24]

    def load_evidence(self, wiki_ids: list[str], provider: str) -> list[KnowledgeEvidence]:
        pages = self.pages()
        return [
            KnowledgeEvidence(
                evidence_id=f"wiki:{wiki_id}", provider=provider, kind="wiki_page",
                title=pages[wiki_id].title, content=pages[wiki_id].content,
                source_file=str(pages[wiki_id].path.relative_to(self.wiki_dir)), wiki_id=wiki_id,
            )
            for wiki_id in wiki_ids
            if wiki_id in pages
        ]


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    marker = text.find("\n---\n", 4)
    if marker < 0:
        return {}, text
    metadata: dict[str, str] = {}
    for line in text[4:marker].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip().strip('"')
    return metadata, text[marker + 5 :]
