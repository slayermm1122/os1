# Knowledge Search Self-Verification SOP

This is the mandatory retrieval-quality loop for OS1 knowledge work. It evaluates
whether `llm_search` and `lex_search` select the right source material before an
answer model is involved.

The loop is source retrieval evaluation, not question answering evaluation. A
golden record contains a question and the source IDs needed to answer it. It does
not contain a prose answer.

## When This Is Required

Run this SOP whenever any of the following is true:

1. A new raw document has been split into canonical chunks and compiled into wiki
   pages and the wiki index.
2. Existing chunks, wiki pages, stable IDs, metadata, or catalog descriptions
   change.
3. The user asks for a one-off self-test of a particular article or topic.
4. Retrieval behavior changes, including query construction, FTS tokenization or
   weights, ranking, selector prompt, selector model, timeout, result limits,
   deduplication, or provider orchestration.

A knowledge implementation is not complete until the applicable loop has been
run and its result has been reported. If an external model or key is unavailable,
run the lexical side, report the LLM side as blocked, and run it when the
dependency becomes available. Never manufacture a passing result.

## Human Judgment Is Mandatory

The golden set must be prepared by a person or by the working agent acting as a
careful human reviewer after reading the raw material, chunks, and wiki pages.
This is not a step that the retrieval system may perform for itself.

The reviewer must:

- choose questions that represent useful facts or concepts in the source;
- inspect the actual wiki and chunk contents before assigning source IDs;
- label `llm_search` wiki IDs and `lex_search` chunk IDs independently;
- include every source genuinely required, but not merely related sources;
- resolve ambiguous or overlapping sources by reviewing the raw material;
- keep the labels fixed while evaluating a retrieval change.

Do not derive golden labels from current search output. Do not ask the selector
model to grade its own selections. Do not modify labels just to improve a metric.
If review proves a label was wrong, correct it explicitly and record that the
golden set changed.

## Golden Set Contract

The default minimum for each raw document or ad-hoc topic scope is five human-
curated cases. Each case contains exactly three English phrasings of the same
information need, producing at least fifteen evaluated queries per scope.

The three phrasings should vary naturally rather than swapping a single word:

- a direct factual or conceptual question;
- a structural or explanatory rephrasing;
- a realistic conversational or task-oriented rephrasing.

Store local datasets under the git-ignored `knowledge/evals/` directory using a
stable scope name:

```json
{
  "schema_version": 1,
  "document_id": "attention_is_all_you_need",
  "description": "Human-curated source-retrieval golden set.",
  "cases": [
    {
      "case_id": "multi_head_attention",
      "queries": [
        "How does multi-head attention work?",
        "Why are queries, keys, and values projected into several heads?",
        "How many attention heads does the base Transformer use?"
      ],
      "relevant": {
        "llm_search": ["transformer.multi_head_attention"],
        "lex_search": ["attention_is_all_you_need:000004"]
      }
    }
  ]
}
```

For a topic spanning multiple documents, `document_id` is the stable evaluation
scope identifier and the relevant lists may contain sources from every document
in that scope. Temporary datasets may live outside the repository and be passed
with `--dataset`; preserve them locally when the result needs to be reproduced.
Raw documents, chunks, wiki pages, and golden datasets are user-owned knowledge
and must not be committed.

The evaluator validates every labeled wiki and chunk ID against the active
corpus before making provider calls. Missing or renamed sources fail the run.

## Execution

Run providers independently. The search-only runner calls the
`KnowledgeSearchProvider` interface directly and does not execute STT, KV
Conversation enrichment, answer generation, TTS, or the normal turn workflow.

```bash
.venv/bin/python -m backend.evals.knowledge_search --provider lex --dataset knowledge/evals/my_document.golden.json
.venv/bin/python -m backend.evals.knowledge_search --provider llm --dataset knowledge/evals/my_document.golden.json
```

Run both in one command when convenient:

```bash
.venv/bin/python -m backend.evals.knowledge_search --provider all --dataset knowledge/evals/my_document.golden.json
```

Useful options:

```bash
--dataset path/to/scope.golden.json
--timeout 5
--db data/search_eval.sqlite
--no-db
```

Use the same timeout and provider configuration as the product when measuring
production behavior. A separate experimental timeout is allowed, but it must be
reported as such and must not replace the production-configured run.

## Metrics

Metrics are computed per query from the ranked stable IDs, then macro-averaged
across all query variants for each provider separately.

- **Recall@5**: fraction of labeled relevant sources present in the first five
  retrieved results.
- **Precision**: fraction of the returned top-five results that are labeled
  relevant. The denominator is the number actually returned, up to five.
- **MRR**: reciprocal rank of the first relevant result, or zero when none is
  retrieved.
- **nDCG@5**: binary-relevance discounted cumulative gain through rank five,
  normalized by the ideal ordering.

Timeouts and provider errors produce an empty ranking and therefore zero
retrieval metrics. They must also be reported separately; otherwise a quality
failure and an availability failure cannot be distinguished.

There is intentionally no universal pass threshold yet. Always report the four
metrics, timeout count, error count, model/index configuration, deadline, and
comparison with the latest applicable baseline. A regression must be explained
before the retrieval change is accepted.

## Persistence And Review

By default, evaluation runs are written to the ignored local database
`data/search_eval.sqlite`:

- `search_eval_runs` stores provider, model, configuration-level summary,
  duration, query count, timeout/error counts, and macro metrics.
- `search_eval_queries` stores the exact query, golden IDs, ranked returned IDs,
  latency, status, error text, and per-query metrics.

After a run, inspect both aggregate and per-query results. Classify failures as:

- source not recalled;
- relevant source ranked too low;
- excessive unrelated results;
- selector timeout;
- provider or parsing error;
- genuinely incorrect or incomplete golden label.

Do not optimize from aggregate scores alone. Read the failed queries and source
artifacts before changing prompts, weights, chunks, wiki pages, or labels.

## Completion Checklist

- [ ] Raw material, chunks, wiki pages, and index were reviewed.
- [ ] At least five cases were labeled by hand for the evaluation scope.
- [ ] Each case has three meaning-preserving query variants.
- [ ] Wiki and chunk truth IDs were labeled independently.
- [ ] Golden IDs validate against the active corpus.
- [ ] `lex_search` ran independently.
- [ ] `llm_search` ran independently with production configuration.
- [ ] Recall@5, precision, MRR, nDCG@5, timeouts, and errors were reported.
- [ ] Run details were persisted unless the run was explicitly ephemeral.
- [ ] Failed queries were inspected and classified.
- [ ] The result was compared with the relevant baseline before completion.
