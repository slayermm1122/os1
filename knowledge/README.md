# OS1 Knowledge Workspace

This directory separates four kinds of knowledge state:

- `raw/` contains immutable source documents.
- `chunks/` contains the canonical JSONL retrieval corpus.
- `wiki/index.md` is the compact selector catalog.
- `wiki/pages/` contains LLM-oriented, sourced synthesis pages.
- `evals/` contains human-curated source-retrieval golden sets.

`data/knowledge.sqlite` is disposable. OS1 validates the JSONL files and rebuilds
the weighted FTS5 index whenever the corpus hash or index schema changes.

## Local Data Boundary

Everything under `raw/`, `chunks/`, `wiki/`, `evals/`, and `.status/` belongs to
the local user and is ignored by git. Generated SQLite indexes live under
`data/` and are also ignored. A repository clone intentionally starts with an
empty knowledge base; users supply and compile their own documents.

## JSONL Contract

Every non-empty line is one JSON object with:

```text
chunk_id, doc_id, document_title, section_title, title_path, text,
source_file, page_start, page_end, chunk_order, language
```

v0.03 accepts only `language: "en"`. Chunk and document IDs are stable; wiki
pages cite chunk IDs and PDF pages so derived claims remain traceable.

## Search Self-Verification

The normative workflow is documented in
[`docs/knowledge-search-self-verification.md`](../docs/knowledge-search-self-verification.md).
It is mandatory after preparing or changing knowledge artifacts and for ad-hoc
article or topic retrieval checks.

Each raw document must provide one golden file under `evals/`. A case contains
three paraphrases and separate relevant source IDs for `llm_search` and
`lex_search`; it deliberately does not contain an answer string.

Run either provider without invoking voice, answer generation, or TTS:

```bash
.venv/bin/python -m backend.evals.knowledge_search --provider lex --dataset knowledge/evals/my_document.golden.json
.venv/bin/python -m backend.evals.knowledge_search --provider llm --dataset knowledge/evals/my_document.golden.json
.venv/bin/python -m backend.evals.knowledge_search --provider all --dataset knowledge/evals/my_document.golden.json
```

The report includes macro Recall@5, precision over the returned top-five list,
MRR, and nDCG@5. Runs and per-query results are saved to the ignored local file
`data/search_eval.sqlite`; pass `--no-db` for an ephemeral run.
