from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import ROOT_DIR, Settings
from ..gateways.ai import XAIGateway
from ..gateways.knowledge import LLMSearch, LexSearch, SQLiteFTSKnowledgeGateway, WikiCatalog
from ..gateways.knowledge.base import KnowledgeEvidence, KnowledgeSearchProvider
from ..gateways.knowledge.sqlite_fts import load_jsonl_chunks
from ..telemetry import SQLiteTelemetryRecorder


DEFAULT_DB = ROOT_DIR / "data" / "search_eval.sqlite"
EVAL_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GoldenQuery:
    query_id: str
    case_id: str
    variant: int
    text: str
    relevant: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class GoldenDataset:
    path: Path
    document_id: str
    description: str
    queries: tuple[GoldenQuery, ...]


@dataclass(frozen=True)
class RetrievalMetrics:
    recall_at_5: float
    precision: float
    mrr: float
    ndcg_at_5: float


@dataclass(frozen=True)
class SearchOnlyResult:
    provider: str
    status: str
    ranked_ids: tuple[str, ...]
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True)
class EvaluatedQuery:
    golden: GoldenQuery
    search: SearchOnlyResult
    metrics: RetrievalMetrics


class SearchOnlyRunner:
    """Runs one knowledge provider without invoking answer, STT, TTS, or conversation code."""

    def __init__(self, *, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds

    async def search(
        self,
        provider: KnowledgeSearchProvider,
        query: str,
        *,
        api_key: str | None,
        cache_key: str,
    ) -> SearchOnlyResult:
        started = time.perf_counter()
        try:
            evidence = await asyncio.wait_for(
                provider.search(
                    query,
                    history=[],
                    api_key=api_key,
                    cache_key=cache_key,
                    trace=None,
                ),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            return SearchOnlyResult(
                provider=provider.provider,
                status="timeout",
                ranked_ids=(),
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            return SearchOnlyResult(
                provider=provider.provider,
                status="error",
                ranked_ids=(),
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
            )
        return SearchOnlyResult(
            provider=provider.provider,
            status="success",
            ranked_ids=tuple(_evidence_id(item) for item in evidence),
            latency_ms=(time.perf_counter() - started) * 1000,
        )


def load_golden_dataset(path: Path) -> GoldenDataset:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not load golden dataset {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != EVAL_SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported or missing schema_version")
    document_id = _nonempty_string(value.get("document_id"), "document_id", path)
    description = str(value.get("description") or "")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path}: cases must be a non-empty list")

    queries: list[GoldenQuery] = []
    case_ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError(f"{path}: every case must be an object")
        case_id = _nonempty_string(case.get("case_id"), "case_id", path)
        if case_id in case_ids:
            raise ValueError(f"{path}: duplicate case_id {case_id}")
        case_ids.add(case_id)
        variants = case.get("queries")
        if not isinstance(variants, list) or len(variants) != 3:
            raise ValueError(f"{path}: {case_id} must contain exactly three queries")
        relevant_value = case.get("relevant")
        if not isinstance(relevant_value, dict):
            raise ValueError(f"{path}: {case_id} must define relevant sources")
        relevant: dict[str, tuple[str, ...]] = {}
        for provider in ("llm_search", "lex_search"):
            ids = relevant_value.get(provider)
            if not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item for item in ids):
                raise ValueError(f"{path}: {case_id}.{provider} must contain source IDs")
            if len(ids) != len(set(ids)):
                raise ValueError(f"{path}: {case_id}.{provider} contains duplicate IDs")
            relevant[provider] = tuple(ids)
        for index, query in enumerate(variants, 1):
            text = _nonempty_string(query, f"{case_id}.queries[{index - 1}]", path)
            queries.append(GoldenQuery(
                query_id=f"{case_id}:{index}",
                case_id=case_id,
                variant=index,
                text=text,
                relevant=relevant,
            ))
    return GoldenDataset(path.resolve(), document_id, description, tuple(queries))


def validate_source_ids(dataset: GoldenDataset, settings: Settings) -> None:
    wiki_ids = set(WikiCatalog(settings.knowledge_root_dir / "wiki").pages())
    chunk_ids = {
        str(row["chunk_id"])
        for row in load_jsonl_chunks(settings.knowledge_root_dir / "chunks")
    }
    missing_wiki: set[str] = set()
    missing_chunks: set[str] = set()
    for query in dataset.queries:
        missing_wiki.update(set(query.relevant["llm_search"]) - wiki_ids)
        missing_chunks.update(set(query.relevant["lex_search"]) - chunk_ids)
    if missing_wiki or missing_chunks:
        details = []
        if missing_wiki:
            details.append(f"unknown wiki IDs: {', '.join(sorted(missing_wiki))}")
        if missing_chunks:
            details.append(f"unknown chunk IDs: {', '.join(sorted(missing_chunks))}")
        raise ValueError("Golden dataset contains " + "; ".join(details))


def retrieval_metrics(
    relevant_ids: Iterable[str], ranked_ids: Iterable[str], *, k: int = 5
) -> RetrievalMetrics:
    relevant = set(relevant_ids)
    ranked = list(dict.fromkeys(ranked_ids))[:k]
    if not relevant:
        raise ValueError("At least one relevant source ID is required")
    hit_count = sum(item in relevant for item in ranked)
    recall = hit_count / len(relevant)
    precision = hit_count / len(ranked) if ranked else 0.0
    reciprocal_rank = next(
        (1.0 / rank for rank, item in enumerate(ranked, 1) if item in relevant),
        0.0,
    )
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, item in enumerate(ranked, 1)
        if item in relevant
    )
    ideal_count = min(len(relevant), k)
    ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return RetrievalMetrics(recall, precision, reciprocal_rank, dcg / ideal_dcg)


async def evaluate_provider(
    dataset: GoldenDataset,
    provider: KnowledgeSearchProvider,
    *,
    truth_key: str,
    api_key: str | None,
    timeout_seconds: float,
) -> list[EvaluatedQuery]:
    runner = SearchOnlyRunner(timeout_seconds=timeout_seconds)
    evaluated: list[EvaluatedQuery] = []
    cache_key = f"search-eval:{dataset.document_id}:{provider.provider}"
    for golden in dataset.queries:
        result = await runner.search(
            provider,
            golden.text,
            api_key=api_key,
            cache_key=cache_key,
        )
        evaluated.append(EvaluatedQuery(
            golden=golden,
            search=result,
            metrics=retrieval_metrics(golden.relevant[truth_key], result.ranked_ids),
        ))
    return evaluated


def aggregate_metrics(results: list[EvaluatedQuery]) -> RetrievalMetrics:
    if not results:
        return RetrievalMetrics(0.0, 0.0, 0.0, 0.0)
    count = len(results)
    return RetrievalMetrics(
        sum(item.metrics.recall_at_5 for item in results) / count,
        sum(item.metrics.precision for item in results) / count,
        sum(item.metrics.mrr for item in results) / count,
        sum(item.metrics.ndcg_at_5 for item in results) / count,
    )


class SearchEvalStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save(
        self,
        dataset: GoldenDataset,
        provider: str,
        model: str,
        results: list[EvaluatedQuery],
        *,
        started_at: str,
        duration_ms: float,
    ) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        metrics = aggregate_metrics(results)
        completed_at = _utc_now()
        with sqlite3.connect(self.path) as conn:
            conn.executescript(_EVAL_SCHEMA)
            conn.execute(
                "INSERT INTO search_eval_runs(run_id, dataset_path, document_id, provider, model, "
                "k, status, started_at, completed_at, duration_ms, query_count, timeout_count, "
                "error_count, recall_at_k, precision, mrr, ndcg_at_k) "
                "VALUES (?, ?, ?, ?, ?, 5, 'complete', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    str(dataset.path),
                    dataset.document_id,
                    provider,
                    model,
                    started_at,
                    completed_at,
                    duration_ms,
                    len(results),
                    sum(item.search.status == "timeout" for item in results),
                    sum(item.search.status == "error" for item in results),
                    metrics.recall_at_5,
                    metrics.precision,
                    metrics.mrr,
                    metrics.ndcg_at_5,
                ),
            )
            conn.executemany(
                "INSERT INTO search_eval_queries(run_id, query_id, case_id, variant, query_text, "
                "status, latency_ms, relevant_json, retrieved_json, recall_at_k, precision, mrr, "
                "ndcg_at_k, error_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        item.golden.query_id,
                        item.golden.case_id,
                        item.golden.variant,
                        item.golden.text,
                        item.search.status,
                        item.search.latency_ms,
                        json.dumps(item.golden.relevant[_truth_key(item.search.provider)]),
                        json.dumps(item.search.ranked_ids),
                        item.metrics.recall_at_5,
                        item.metrics.precision,
                        item.metrics.mrr,
                        item.metrics.ndcg_at_5,
                        item.search.error,
                    )
                    for item in results
                ],
            )
            conn.execute(f"PRAGMA user_version={EVAL_SCHEMA_VERSION}")
            conn.commit()
        return run_id


async def run(args: argparse.Namespace) -> int:
    dataset = load_golden_dataset(args.dataset)
    base = Settings()
    eval_settings = Settings(
        root_dir=base.root_dir,
        frontend_dir=base.frontend_dir,
        knowledge_limit=5,
    )
    validate_source_ids(dataset, eval_settings)
    providers: list[tuple[str, KnowledgeSearchProvider, str, str | None]] = []
    if args.provider in {"lex", "all"}:
        index = SQLiteFTSKnowledgeGateway(eval_settings)
        index.ensure_index()
        providers.append(("lex_search", LexSearch(index), "sqlite_fts5", None))
    if args.provider in {"llm", "all"}:
        if not eval_settings.llm_api_key:
            raise ValueError("LLM search evaluation requires the configured xAI API key")
        ai = XAIGateway(
            eval_settings,
            model=eval_settings.knowledge_selector_model,
            reasoning_effort=eval_settings.knowledge_selector_reasoning_effort,
        )
        llm = LLMSearch(
            ai,
            WikiCatalog(eval_settings.knowledge_root_dir / "wiki"),
            SQLiteTelemetryRecorder(args.db, enabled=False),
            limit=eval_settings.knowledge_wiki_limit,
        )
        providers.append(("llm_search", llm, ai.model, eval_settings.llm_api_key))

    store = SearchEvalStore(args.db) if not args.no_db else None
    for truth_key, provider, model, api_key in providers:
        started_at = _utc_now()
        started = time.perf_counter()
        results = await evaluate_provider(
            dataset,
            provider,
            truth_key=truth_key,
            api_key=api_key,
            timeout_seconds=args.timeout,
        )
        duration_ms = (time.perf_counter() - started) * 1000
        _print_report(truth_key, provider.provider, model, results, duration_ms)
        if store:
            run_id = store.save(
                dataset,
                provider.provider,
                model,
                results,
                started_at=started_at,
                duration_ms=duration_ms,
            )
            print(f"sqlite_run={run_id} db={store.path}")
    return 0


def _print_report(
    truth_key: str,
    provider: str,
    model: str,
    results: list[EvaluatedQuery],
    duration_ms: float,
) -> None:
    metrics = aggregate_metrics(results)
    print(f"\n{truth_key} provider={provider} model={model} queries={len(results)} duration_ms={duration_ms:.1f}")
    print("query_id                              status     ms      R@5   P     MRR   nDCG@5  retrieved")
    for item in results:
        print(
            f"{item.golden.query_id:<37} {item.search.status:<10} "
            f"{item.search.latency_ms:>6.1f}  {item.metrics.recall_at_5:.3f} "
            f"{item.metrics.precision:.3f} {item.metrics.mrr:.3f} "
            f"{item.metrics.ndcg_at_5:.3f}  {list(item.search.ranked_ids)}"
        )
    print(
        f"macro recall@5={metrics.recall_at_5:.3f} precision={metrics.precision:.3f} "
        f"mrr={metrics.mrr:.3f} ndcg@5={metrics.ndcg_at_5:.3f} "
        f"timeouts={sum(item.search.status == 'timeout' for item in results)} "
        f"errors={sum(item.search.status == 'error' for item in results)}"
    )


def _evidence_id(evidence: KnowledgeEvidence) -> str:
    return evidence.wiki_id or evidence.chunk_id or evidence.evidence_id


def _truth_key(provider: str) -> str:
    return "llm_search" if provider.startswith("llm_search") else "lex_search"


def _nonempty_string(value: object, field: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {field} must be a non-empty string")
    return value.strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate OS1 knowledge search without answer generation")
    parser.add_argument("--provider", choices=("lex", "llm", "all"), default="all")
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Path to a local human-curated golden dataset",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--timeout", type=float, default=Settings().knowledge_search_timeout_seconds)
    parser.add_argument("--no-db", action="store_true", help="Do not persist this evaluation run")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return args


def main() -> int:
    try:
        return asyncio.run(run(_arguments()))
    except (OSError, ValueError) as exc:
        print(f"evaluation failed: {exc}")
        return 2


_EVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_eval_runs (
    run_id TEXT PRIMARY KEY,
    dataset_path TEXT NOT NULL,
    document_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    k INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    query_count INTEGER NOT NULL,
    timeout_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    recall_at_k REAL NOT NULL,
    precision REAL NOT NULL,
    mrr REAL NOT NULL,
    ndcg_at_k REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS search_eval_queries (
    run_id TEXT NOT NULL REFERENCES search_eval_runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    variant INTEGER NOT NULL,
    query_text TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    relevant_json TEXT NOT NULL,
    retrieved_json TEXT NOT NULL,
    recall_at_k REAL NOT NULL,
    precision REAL NOT NULL,
    mrr REAL NOT NULL,
    ndcg_at_k REAL NOT NULL,
    error_text TEXT,
    PRIMARY KEY (run_id, query_id)
);
CREATE INDEX IF NOT EXISTS idx_search_eval_provider_started
ON search_eval_runs(provider, started_at);
"""


if __name__ == "__main__":
    raise SystemExit(main())
