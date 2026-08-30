"""检索指标（说明书 §16）。

Recall@K、MRR@K、citation validity、abstention 正确率。全部是纯函数：
输入是查询集与检索结果，不做 IO。M6 的评测 harness 复用它们。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalQuery:
    """一条检索评测查询。

    relevant_section_ids 是**人工标注**的相关段落集合。它必须与语料一起冻结
    （M6 冻结 dataset 时冻结的包括这份标注），否则 Recall 的分母会变。
    """

    query_id: str
    text: str
    service: str | None
    relevant_section_ids: frozenset[str]
    # 期望「检索不到」的查询。S7（Runbook 缺失）依赖它——
    # 一个只测「能找到」的评测集无法证明系统会正确弃答。
    expects_miss: bool = False


@dataclass(frozen=True)
class RetrievalMetrics:
    queries: int
    recall_at_k: float
    mrr_at_k: float
    k: int
    hit_queries: int
    miss_queries: int
    # 期望未命中的查询里，确实未命中的比例。
    correct_abstention_rate: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "queries": self.queries,
            "k": self.k,
            "recall_at_k": round(self.recall_at_k, 4),
            "mrr_at_k": round(self.mrr_at_k, 4),
            "hit_queries": self.hit_queries,
            "miss_queries": self.miss_queries,
            "correct_abstention_rate": round(self.correct_abstention_rate, 4),
        }


def recall_at_k(retrieved_ids: list[str], relevant: frozenset[str], k: int) -> float:
    """命中的相关段落占全部相关段落的比例。

    用「召回了多少相关项」而不是「是否至少召回一个」：后者是 hit rate，
    在一个查询有多个相关段落时会高估检索质量。
    """
    if not relevant:
        return 0.0
    top = retrieved_ids[:k]
    found = sum(1 for section_id in relevant if section_id in top)
    return found / len(relevant)


def reciprocal_rank(retrieved_ids: list[str], relevant: frozenset[str], k: int) -> float:
    """第一个相关结果的排名倒数。没有相关结果则为 0。"""
    for index, section_id in enumerate(retrieved_ids[:k], start=1):
        if section_id in relevant:
            return 1.0 / index
    return 0.0


def evaluate(
    queries: list[RetrievalQuery],
    results: dict[str, list[str]],
    *,
    k: int,
) -> RetrievalMetrics:
    """results: query_id -> 有序的 section_id 列表。

    期望未命中的查询**不计入** recall/MRR：它们没有相关段落，算进去会把分母污染。
    它们单独统计为 correct_abstention_rate。
    """
    scored = [q for q in queries if not q.expects_miss]
    miss_expected = [q for q in queries if q.expects_miss]

    recalls: list[float] = []
    rrs: list[float] = []
    hits = 0
    for query in scored:
        retrieved = results.get(query.query_id, [])
        r = recall_at_k(retrieved, query.relevant_section_ids, k)
        recalls.append(r)
        rr = reciprocal_rank(retrieved, query.relevant_section_ids, k)
        rrs.append(rr)
        if rr > 0:
            hits += 1

    correct_abstentions = sum(
        1 for query in miss_expected if not results.get(query.query_id)
    )
    return RetrievalMetrics(
        queries=len(queries),
        recall_at_k=(sum(recalls) / len(recalls)) if recalls else 0.0,
        mrr_at_k=(sum(rrs) / len(rrs)) if rrs else 0.0,
        k=k,
        hit_queries=hits,
        miss_queries=len(scored) - hits,
        correct_abstention_rate=(
            correct_abstentions / len(miss_expected) if miss_expected else 1.0
        ),
    )


def compare(baseline: RetrievalMetrics, candidate: RetrievalMetrics) -> dict[str, float]:
    """基线与候选的差值。

    说明书 §10：「没有基线就说不清 embedding 到底带来了什么」。这个函数存在的意义
    就是让那个「什么」是一个可报告的数字，而不是一句主张。
    """
    return {
        "recall_at_k_delta": round(candidate.recall_at_k - baseline.recall_at_k, 4),
        "mrr_at_k_delta": round(candidate.mrr_at_k - baseline.mrr_at_k, 4),
        "hit_queries_delta": candidate.hit_queries - baseline.hit_queries,
    }


# -- 引用校验 --------------------------------------------------------------


@dataclass(frozen=True)
class CitationCheck:
    total: int
    valid: int
    invalid_reasons: tuple[str, ...]

    @property
    def validity(self) -> float:
        return self.valid / self.total if self.total else 1.0


def verify_citations(
    citations: list[dict[str, str]],
    *,
    known_chunks: dict[tuple[str, str], str],
    known_sections: frozenset[str] | None = None,
) -> CitationCheck:
    """校验每条引用能否反查到原文。

    known_chunks 的键是 **(section_id, document_version)** 而非 section_id：
    多版本语料下按 section_id 索引会让后加载的版本覆盖先前的，任何指向旧版本的
    引用都反查不到——而「旧 Run 的引用仍指向旧版本」正是 UJ5 要求的性质。
    版本进 key 之后，「section 对但版本错」在结构上就查不到，不需要单独比对。

    known_sections 只用于区分错误原因（section 不存在 vs 版本不存在），
    使失败信息能指出问题在哪一层。
    """
    sections = known_sections or frozenset(section for section, _ in known_chunks)
    invalid: list[str] = []
    valid = 0
    for citation in citations:
        section_id = citation.get("section_id", "")
        version = citation.get("document_version", "")
        if section_id not in sections:
            invalid.append(f"{section_id or '<empty>'}: section_id not found")
            continue
        expected_hash = known_chunks.get((section_id, version))
        if expected_hash is None:
            invalid.append(
                f"{section_id}: document_version {version!r} does not exist for this section"
            )
            continue
        if citation.get("content_hash") != expected_hash:
            invalid.append(f"{section_id}: content_hash does not match stored content")
            continue
        valid += 1
    return CitationCheck(total=len(citations), valid=valid, invalid_reasons=tuple(invalid))
