from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.config import Settings
from backend.evals.knowledge_search import (
    SearchOnlyRunner,
    load_golden_dataset,
    retrieval_metrics,
    validate_source_ids,
)
from backend.gateways.knowledge import KnowledgeEvidence, SQLiteFTSKnowledgeGateway


class StaticProvider:
    provider = "static"

    def __init__(self, results=None, *, delay: float = 0) -> None:
        self.results = results or []
        self.delay = delay
        self.cancelled = False

    async def search(self, *args, **kwargs):
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.results
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class SearchEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_local_golden_set_has_five_cases_and_fifteen_valid_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chunks = root / "knowledge" / "chunks"
            pages = root / "knowledge" / "wiki" / "pages"
            evals = root / "knowledge" / "evals"
            chunks.mkdir(parents=True)
            pages.mkdir(parents=True)
            evals.mkdir(parents=True)
            chunk = {
                "chunk_id": "paper:000001",
                "doc_id": "paper",
                "document_title": "Paper",
                "section_title": "Attention",
                "title_path": "Paper > Attention",
                "text": "Attention uses learned projections.",
                "source_file": "paper.pdf",
                "page_start": 1,
                "page_end": 1,
                "chunk_order": 1,
                "language": "en",
            }
            (chunks / "paper.jsonl").write_text(json.dumps(chunk) + "\n", encoding="utf-8")
            (root / "knowledge" / "wiki" / "index.md").write_text(
                "- `paper.attention` — Attention.\n", encoding="utf-8"
            )
            (pages / "attention.md").write_text(
                "---\nid: paper.attention\ntitle: Attention\nsummary: Attention.\n---\nBody\n",
                encoding="utf-8",
            )
            cases = [
                {
                    "case_id": f"case_{index}",
                    "queries": [f"Question {index} variant {variant}" for variant in range(1, 4)],
                    "relevant": {
                        "llm_search": ["paper.attention"],
                        "lex_search": ["paper:000001"],
                    },
                }
                for index in range(1, 6)
            ]
            path = evals / "paper.golden.json"
            path.write_text(
                json.dumps({"schema_version": 1, "document_id": "paper", "cases": cases}),
                encoding="utf-8",
            )
            settings = Settings(
                root_dir=root,
                frontend_dir=root / "frontend",
                knowledge_root_dir=root / "knowledge",
                knowledge_docs_dir=root / "knowledge" / "raw",
                knowledge_db_path=root / "data" / "knowledge.sqlite",
            )
            dataset = load_golden_dataset(path)
            self.assertEqual(dataset.document_id, "paper")
            self.assertEqual(len(dataset.queries), 15)
            self.assertEqual(len({query.case_id for query in dataset.queries}), 5)
            validate_source_ids(dataset, settings)

    def test_metrics_use_binary_relevance_and_rank_order(self) -> None:
        result = retrieval_metrics(["a", "b"], ["x", "b", "a"], k=5)
        self.assertEqual(result.recall_at_5, 1.0)
        self.assertAlmostEqual(result.precision, 2 / 3)
        self.assertEqual(result.mrr, 0.5)
        expected_dcg = (1 / math.log2(3) + 1 / math.log2(4)) / (1 + 1 / math.log2(3))
        self.assertAlmostEqual(result.ndcg_at_5, expected_dcg)

    async def test_search_only_runner_returns_ids_and_bounds_timeout(self) -> None:
        evidence = KnowledgeEvidence(
            evidence_id="chunk:one",
            provider="static",
            kind="raw_chunk",
            title="One",
            content="body",
            chunk_id="one",
        )
        runner = SearchOnlyRunner(timeout_seconds=0.02)
        success = await runner.search(
            StaticProvider([evidence]), "query", api_key=None, cache_key="eval"
        )
        self.assertEqual(success.status, "success")
        self.assertEqual(success.ranked_ids, ("one",))

        slow = StaticProvider(delay=1)
        timeout = await runner.search(slow, "query", api_key=None, cache_key="eval")
        self.assertEqual(timeout.status, "timeout")
        self.assertTrue(slow.cancelled)

    def test_index_check_rebuilds_when_metadata_hash_matches_but_rows_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chunks = root / "knowledge" / "chunks"
            chunks.mkdir(parents=True)
            row = {
                "chunk_id": "paper:000001",
                "doc_id": "paper",
                "document_title": "Paper",
                "section_title": "Attention",
                "title_path": "Paper > Attention",
                "text": "Multi-head attention uses several learned projections.",
                "source_file": "paper.pdf",
                "page_start": 1,
                "page_end": 1,
                "chunk_order": 1,
                "language": "en",
            }
            (chunks / "paper.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            settings = Settings(
                root_dir=root,
                frontend_dir=root / "frontend",
                knowledge_root_dir=root / "knowledge",
                knowledge_docs_dir=root / "knowledge" / "raw",
                knowledge_db_path=root / "data" / "knowledge.sqlite",
            )
            gateway = SQLiteFTSKnowledgeGateway(settings)
            self.assertEqual(gateway.reindex(), 1)
            with sqlite3.connect(settings.knowledge_db_path) as conn:
                conn.execute("DELETE FROM docs_fts")
                conn.commit()
            gateway.ensure_index()
            self.assertEqual(
                gateway.search_evidence("learned projections")[0].chunk_id,
                "paper:000001",
            )


if __name__ == "__main__":
    unittest.main()
