# OS1 Knowledge Workspace

This directory separates four kinds of knowledge state:

- `raw/` contains immutable source documents.
- `chunks/` contains the canonical JSONL retrieval corpus.
- `wiki/index.md` is the compact selector catalog.
- `wiki/pages/` contains LLM-oriented, sourced synthesis pages.
- `evals/` contains human-curated source-retrieval golden sets.

`data/knowledge.sqlite` is disposable. OS1 validates the JSONL files and rebuilds
the weighted FTS5 index whenever the corpus hash or index schema changes.

## Bundled v0.03 Fixture

The initial source is [Attention Is All You Need](https://arxiv.org/abs/1706.03762)
by Vaswani et al. The bundled PDF was downloaded from arXiv and has SHA-256:

```text
bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697
```

The nine English chunks and five wiki pages are hand-compiled test artifacts.
They provide a deterministic golden corpus for a future ingestion architecture;
upload and compilation are intentionally outside the current implementation.

## JSONL Contract

Every non-empty line is one JSON object with:

```text
chunk_id, doc_id, document_title, section_title, title_path, text,
source_file, page_start, page_end, chunk_order, language
```

v0.03 accepts only `language: "en"`. Chunk and document IDs are stable; wiki
pages cite chunk IDs and PDF pages so derived claims remain traceable.

## Search Self-Verification

Each raw document may provide one golden file under `evals/`. A case contains
three paraphrases and separate relevant source IDs for `llm_search` and
`lex_search`; it deliberately does not contain an answer string.

Run either provider without invoking voice, answer generation, or TTS:

```bash
.venv/bin/python -m backend.evals.knowledge_search --provider lex
.venv/bin/python -m backend.evals.knowledge_search --provider llm
.venv/bin/python -m backend.evals.knowledge_search --provider all
```

The report includes macro Recall@5, precision over the returned top-five list,
MRR, and nDCG@5. Runs and per-query results are saved to the ignored local file
`data/search_eval.sqlite`; pass `--no-db` for an ephemeral run.
