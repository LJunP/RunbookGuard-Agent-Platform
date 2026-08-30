#!/usr/bin/env python3
"""检索评测：基线 vs 混合（说明书 §10）。

顺序是硬要求：先报 BM25 基线的数字，再报混合检索的数字，最后报差值。
没有基线就说不清 embedding 带来了什么。

用法：
    python scripts/eval-retrieval.py                    # 只跑词法基线
    python scripts/eval-retrieval.py --with-vector      # 加向量（首次会下载模型）
    python scripts/eval-retrieval.py --qdrant           # 向量走 Qdrant 而非内存
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "apps" / "agent-runtime-python" / "src"))

from agent_runtime.retrieval.eval_queries import (  # noqa: E402
    ALL_QUERIES,
    DIAGNOSTIC_QUERIES,
    MISS_QUERIES,
    REMEDIAL_QUERIES,
)
from agent_runtime.retrieval.metrics import compare, evaluate  # noqa: E402
from agent_runtime.retrieval.retrievers import (  # noqa: E402
    Embedder,
    HybridRetriever,
    InMemoryVectorRetriever,
    LexicalRetriever,
    QdrantVectorRetriever,
)
from agent_runtime.retrieval.runbook import load_runbooks  # noqa: E402

CORPUS = REPO / "datasets" / "runbooks"
REPORTS = REPO / "eval" / "reports"
MODEL = "BAAI/bge-small-en-v1.5"
TENANT = "tenant-demo"
K_VALUES = (3, 5, 10)


def run_queries(retriever, queries, *, k: int) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}
    for query in queries:
        hits = retriever.search(
            query.text, tenant_id=TENANT, service=query.service, top_k=k
        )
        results[query.query_id] = [hit.chunk.section_id for hit in hits]
    return results


def report(label: str, retriever, queries) -> dict[str, dict]:
    print(f"\n== {label} ==")
    out: dict[str, dict] = {}
    for k in K_VALUES:
        metrics = evaluate(queries, run_queries(retriever, queries, k=k), k=k)
        out[f"k={k}"] = metrics.as_dict()
        print(
            f"  K={k:<3} Recall={metrics.recall_at_k:.4f}  MRR={metrics.mrr_at_k:.4f}  "
            f"hit={metrics.hit_queries}/{metrics.hit_queries + metrics.miss_queries}  "
            f"abstention={metrics.correct_abstention_rate:.4f}"
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-vector", action="store_true", help="加入向量检索与混合检索")
    parser.add_argument("--qdrant", action="store_true", help="向量检索走 Qdrant")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    args = parser.parse_args()

    books = load_runbooks(CORPUS)
    chunks = [c for b in books for c in b.chunks(embedding_model=MODEL)]
    print(f"corpus: {len(books)} runbooks, {len(chunks)} chunks")
    print(f"queries: {len(DIAGNOSTIC_QUERIES)} diagnostic, "
          f"{len(REMEDIAL_QUERIES)} remedial, {len(MISS_QUERIES)} expected-miss")

    payload: dict[str, object] = {
        "ran_at": datetime.now(UTC).isoformat(),
        "corpus": {"runbooks": len(books), "chunks": len(chunks)},
        "queries": {
            "diagnostic": len(DIAGNOSTIC_QUERIES),
            "remedial": len(REMEDIAL_QUERIES),
            "expected_miss": len(MISS_QUERIES),
        },
        "embedding_model": MODEL,
    }

    lexical = LexicalRetriever(chunks)
    baseline = report("baseline: BM25 lexical only", lexical, ALL_QUERIES)
    payload["baseline_lexical"] = baseline

    if not args.with_vector:
        print("\n向量检索未启用。加 --with-vector 看混合检索的数字与差值。")
        print("说明书 §10 要求基线先行，因此这一步单独可跑。")
        _write(payload)
        return 0

    embedder = Embedder(MODEL)
    if args.qdrant:
        vector = QdrantVectorRetriever(chunks, embedder, url=args.qdrant_url)
        print(f"\nindexing into Qdrant at {args.qdrant_url} ...")
        vector.index()
    else:
        vector = InMemoryVectorRetriever(chunks, embedder)
        print("\nembedding corpus in memory (first run downloads the model) ...")
        vector.index()

    vector_only = report("vector only", vector, ALL_QUERIES)
    payload["vector_only"] = vector_only

    hybrid = HybridRetriever([lexical, vector])
    hybrid_metrics = report("hybrid: RRF fusion + rule rerank", hybrid, ALL_QUERIES)
    payload["hybrid"] = hybrid_metrics

    print("\n== 混合检索相对基线的增量 ==")
    deltas: dict[str, dict] = {}
    for k in K_VALUES:
        base = evaluate(ALL_QUERIES, run_queries(lexical, ALL_QUERIES, k=k), k=k)
        cand = evaluate(ALL_QUERIES, run_queries(hybrid, ALL_QUERIES, k=k), k=k)
        delta = compare(base, cand)
        deltas[f"k={k}"] = delta
        print(
            f"  K={k:<3} ΔRecall={delta['recall_at_k_delta']:+.4f}  "
            f"ΔMRR={delta['mrr_at_k_delta']:+.4f}  "
            f"Δhit={delta['hit_queries_delta']:+d}"
        )
    payload["hybrid_vs_baseline"] = deltas

    # 分组报告：处置类查询用于验证 rerank 的段落类型先验是否真的起作用。
    print("\n== 分组：处置类查询（验证段落类型先验） ==")
    for label, retriever in (("baseline", lexical), ("hybrid", hybrid)):
        metrics = evaluate(
            REMEDIAL_QUERIES, run_queries(retriever, REMEDIAL_QUERIES, k=5), k=5
        )
        print(f"  {label:<9} Recall@5={metrics.recall_at_k:.4f}  MRR@5={metrics.mrr_at_k:.4f}")
        payload.setdefault("remedial_only", {})[label] = metrics.as_dict()

    _write(payload)
    return 0


def _write(payload: dict) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS / f"retrieval-{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nreport written: {path.relative_to(REPO)}")


if __name__ == "__main__":
    raise SystemExit(main())
