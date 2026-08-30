"""检索层的测试。

先写失败路径（铁律二）：元数据缺失、超长段落、跨租户、低相关、版本隔离。
指标计算与检索器分开测——指标是纯函数，检索器需要语料。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.retrieval.metrics import (
    CitationCheck,
    RetrievalQuery,
    compare,
    evaluate,
    recall_at_k,
    reciprocal_rank,
    verify_citations,
)
from agent_runtime.retrieval.retrievers import (
    HybridRetriever,
    LexicalRetriever,
    ScoredChunk,
    tokenize,
)
from agent_runtime.retrieval.runbook import (
    MAX_SECTION_CHARS,
    Runbook,
    RunbookChunk,
    RunbookError,
    SectionKind,
    content_hash_of,
    load_runbooks,
    parse_runbook_markdown,
)

CORPUS = Path(__file__).resolve().parents[3] / "datasets" / "runbooks"
MODEL = "BAAI/bge-small-en-v1.5"


def _sections(**overrides: str) -> dict[SectionKind, str]:
    base = {
        SectionKind.SERVICE: "synthetic-orders",
        SectionKind.SYMPTOM: "latency rises and the pool saturates",
        SectionKind.PRECONDITION: "a fixed size pool is in use",
        SectionKind.DIAGNOSIS: "compare saturation time against the deployment time",
        SectionKind.SAFE_ACTION: "roll back to the previous version",
        SectionKind.ROLLBACK: "redeploy the previous version and verify",
    }
    for key, value in overrides.items():
        base[SectionKind(key)] = value
    return base


def _runbook(**overrides) -> Runbook:
    defaults = dict(
        document_id="rb-test",
        document_version="1.0.0",
        service="synthetic-orders",
        tenant_id="tenant-demo",
        title="Test runbook",
        sections=_sections(),
    )
    defaults.update(overrides)
    return Runbook(**defaults)


def _chunk(**overrides) -> RunbookChunk:
    content = overrides.pop("content", "pool exhausted after the release")
    defaults = dict(
        document_id="rb-test",
        document_version="1.0.0",
        section_id="rb-test#symptom",
        section_kind=SectionKind.SYMPTOM,
        service="synthetic-orders",
        tenant_id="tenant-demo",
        content=content,
        content_hash=content_hash_of(content),
        chunker_version="section-aware-v1",
        embedding_model=MODEL,
        indexed_at="2026-08-29T12:00:00+00:00",
    )
    defaults.update(overrides)
    return RunbookChunk(**defaults)


# --------------------------------------------------------------------------
# 元数据强制（说明书 §16：缺元数据就无法做引用校验）
# --------------------------------------------------------------------------

class TestMetadataIsMandatory:
    @pytest.mark.parametrize(
        "field",
        [
            "document_id",
            "document_version",
            "section_id",
            "service",
            "tenant_id",
            "content_hash",
            "chunker_version",
            "embedding_model",
            "indexed_at",
        ],
    )
    def test_missing_metadata_is_rejected(self, field: str) -> None:
        with pytest.raises(RunbookError, match=field):
            _chunk(**{field: ""})

    def test_empty_content_is_rejected(self) -> None:
        with pytest.raises(RunbookError):
            _chunk(content="")

    def test_no_default_document_version(self) -> None:
        """默认版本号会让两个不同版本的文档看起来同版本。"""
        with pytest.raises(RunbookError, match="document_version"):
            _chunk(document_version="")

    def test_oversized_section_is_rejected_not_truncated(self) -> None:
        """静默截断会让 content_hash 对应不到完整原文。"""
        long_content = "x" * (MAX_SECTION_CHARS + 1)
        with pytest.raises(RunbookError, match="exceeds"):
            _chunk(content=long_content)

    def test_mismatched_content_hash_is_rejected(self) -> None:
        with pytest.raises(RunbookError, match="content_hash"):
            _chunk(content_hash="0" * 64)


class TestRunbookStructure:
    @pytest.mark.parametrize("missing", [k.value for k in SectionKind])
    def test_missing_section_is_rejected(self, missing: str) -> None:
        sections = _sections()
        sections[SectionKind(missing)] = ""
        with pytest.raises(RunbookError, match="missing required sections"):
            _runbook(sections=sections)

    def test_six_chunks_per_runbook(self) -> None:
        chunks = _runbook().chunks(embedding_model=MODEL)
        assert len(chunks) == 6
        assert {c.section_kind for c in chunks} == set(SectionKind)

    def test_section_id_is_document_scoped(self) -> None:
        chunks = _runbook().chunks(embedding_model=MODEL)
        assert all(c.section_id.startswith("rb-test#") for c in chunks)

    def test_point_id_includes_version(self) -> None:
        """同一段落的不同版本必须是不同的 point，否则更新会覆盖旧版本，
        旧 Run 的引用就反查不到了（UJ5 的失败判据）。"""
        v1 = _chunk(document_version="1.0.0")
        v2 = _chunk(document_version="2.0.0")
        assert v1.point_id() != v2.point_id()

    def test_point_id_is_tenant_scoped(self) -> None:
        a = _chunk(tenant_id="tenant-a")
        b = _chunk(tenant_id="tenant-b")
        assert a.point_id() != b.point_id()


class TestMarkdownParsing:
    def test_missing_front_matter_is_rejected(self) -> None:
        with pytest.raises(RunbookError, match="front matter"):
            parse_runbook_markdown("## service\n\nx\n", default_tenant="t")

    def test_unknown_section_is_rejected(self) -> None:
        text = '---\nid: a\nversion: "1"\nservice: s\ntitle: "t"\n---\n\n## nonsense\n\nx\n'
        with pytest.raises(RunbookError, match="unknown section"):
            parse_runbook_markdown(text, default_tenant="t")

    def test_missing_front_matter_field_is_rejected(self) -> None:
        text = '---\nid: a\n---\n\n## service\n\ns\n'
        with pytest.raises(RunbookError, match="version"):
            parse_runbook_markdown(text, default_tenant="t")


class TestCorpus:
    def test_corpus_loads(self) -> None:
        books = load_runbooks(CORPUS)
        assert len(books) >= 30, f"spec requires 30~50 runbooks, got {len(books)}"

    def test_corpus_has_multiple_versions_of_some_documents(self) -> None:
        books = load_runbooks(CORPUS)
        by_id: dict[str, set[str]] = {}
        for book in books:
            by_id.setdefault(book.document_id, set()).add(book.document_version)
        multi = {k: v for k, v in by_id.items() if len(v) > 1}
        assert multi, "need multi-version documents to test citation version isolation"

    def test_every_corpus_chunk_has_full_metadata(self) -> None:
        for book in load_runbooks(CORPUS):
            for chunk in book.chunks(embedding_model=MODEL):
                assert chunk.content_hash == content_hash_of(chunk.content)
                assert chunk.chunker_version and chunk.embedding_model and chunk.indexed_at

    def test_corpus_chunk_count(self) -> None:
        books = load_runbooks(CORPUS)
        chunks = [c for b in books for c in b.chunks(embedding_model=MODEL)]
        assert len(chunks) == len(books) * 6


# --------------------------------------------------------------------------
# 指标（纯函数）
# --------------------------------------------------------------------------

class TestMetrics:
    def test_recall_counts_all_relevant_not_just_first(self) -> None:
        """用 recall 而非 hit rate：一个查询有多个相关段落时 hit rate 会高估。"""
        relevant = frozenset({"a", "b", "c"})
        assert recall_at_k(["a", "x", "b"], relevant, 5) == pytest.approx(2 / 3)

    def test_recall_respects_k(self) -> None:
        relevant = frozenset({"a", "b"})
        assert recall_at_k(["x", "y", "a", "b"], relevant, 2) == 0.0

    def test_reciprocal_rank_uses_first_hit(self) -> None:
        assert reciprocal_rank(["x", "a"], frozenset({"a"}), 5) == 0.5
        assert reciprocal_rank(["a", "x"], frozenset({"a"}), 5) == 1.0

    def test_reciprocal_rank_is_zero_without_hit(self) -> None:
        assert reciprocal_rank(["x", "y"], frozenset({"a"}), 5) == 0.0

    def test_expected_miss_queries_are_excluded_from_recall(self) -> None:
        """期望未命中的查询没有相关段落，算进 recall 会污染分母。"""
        queries = [
            RetrievalQuery("q1", "pool", "synthetic-orders", frozenset({"a"})),
            RetrievalQuery("q2", "nonsense", None, frozenset(), expects_miss=True),
        ]
        metrics = evaluate(queries, {"q1": ["a"], "q2": []}, k=5)
        assert metrics.recall_at_k == 1.0
        assert metrics.correct_abstention_rate == 1.0

    def test_wrong_abstention_is_reported(self) -> None:
        queries = [RetrievalQuery("q1", "nonsense", None, frozenset(), expects_miss=True)]
        metrics = evaluate(queries, {"q1": ["something"]}, k=5)
        assert metrics.correct_abstention_rate == 0.0

    def test_compare_reports_the_delta(self) -> None:
        """说明书 §10：基线存在的意义是让「embedding 带来了什么」成为一个数字。"""
        queries = [RetrievalQuery("q1", "pool", None, frozenset({"a"}))]
        baseline = evaluate(queries, {"q1": ["x", "a"]}, k=5)
        candidate = evaluate(queries, {"q1": ["a", "x"]}, k=5)
        delta = compare(baseline, candidate)
        assert delta["mrr_at_k_delta"] == pytest.approx(0.5)


class TestCitationVerification:
    # key 是 (section_id, document_version)：多版本语料下按 section_id 索引会让
    # 后加载的版本覆盖先前的，旧版本的引用就反查不到了。
    KNOWN = {("rb-a#symptom", "1.0.0"): content_hash_of("s")}

    def test_valid_citation(self) -> None:
        check = verify_citations(
            [
                {
                    "section_id": "rb-a#symptom",
                    "document_version": "1.0.0",
                    "content_hash": content_hash_of("s"),
                }
            ],
            known_chunks=self.KNOWN,
        )
        assert check.validity == 1.0

    def test_unknown_section_is_invalid(self) -> None:
        check = verify_citations(
            [{"section_id": "rb-z#symptom", "document_version": "1.0.0", "content_hash": "x"}],
            known_chunks=self.KNOWN,
        )
        assert check.validity == 0.0
        assert "not found" in check.invalid_reasons[0]

    def test_wrong_version_is_invalid(self) -> None:
        """版本不在索引里就查不到——不需要单独比对版本字段。"""
        check = verify_citations(
            [
                {
                    "section_id": "rb-a#symptom",
                    "document_version": "2.0.0",
                    "content_hash": content_hash_of("s"),
                }
            ],
            known_chunks=self.KNOWN,
        )
        assert check.validity == 0.0
        assert "document_version" in check.invalid_reasons[0]

    def test_wrong_hash_is_invalid(self) -> None:
        check = verify_citations(
            [
                {
                    "section_id": "rb-a#symptom",
                    "document_version": "1.0.0",
                    "content_hash": "0" * 64,
                }
            ],
            known_chunks=self.KNOWN,
        )
        assert check.validity == 0.0
        assert "content_hash" in check.invalid_reasons[0]

    def test_empty_citation_list_is_vacuously_valid(self) -> None:
        """没有引用不等于引用无效——「不引用」的问题由 groundedness 检查捕获。"""
        assert verify_citations([], known_chunks=self.KNOWN).validity == 1.0


# --------------------------------------------------------------------------
# 词法基线
# --------------------------------------------------------------------------

class TestLexicalRetriever:
    @pytest.fixture(scope="class")
    def corpus_chunks(self) -> list[RunbookChunk]:
        return [c for b in load_runbooks(CORPUS) for c in b.chunks(embedding_model=MODEL)]

    def test_finds_pool_exhaustion(self, corpus_chunks) -> None:
        retriever = LexicalRetriever(corpus_chunks)
        results = retriever.search(
            "connection pool exhausted timeout acquiring connection",
            tenant_id="tenant-demo",
            top_k=5,
        )
        assert results
        assert any("rb-db-pool-exhaustion" in r.chunk.document_id for r in results)

    def test_cross_tenant_returns_nothing(self, corpus_chunks) -> None:
        """威胁 T-4：前置过滤让「忘记过滤」表现为查不到，而不是查到别人的。"""
        retriever = LexicalRetriever(corpus_chunks)
        assert retriever.search("connection pool", tenant_id="tenant-b") == []

    def test_service_filter_narrows_results(self, corpus_chunks) -> None:
        retriever = LexicalRetriever(corpus_chunks)
        results = retriever.search(
            "queue backlog", tenant_id="tenant-demo", service="synthetic-notify", top_k=10
        )
        assert results
        assert all(r.chunk.service == "synthetic-notify" for r in results)

    def test_empty_query_returns_nothing(self, corpus_chunks) -> None:
        assert LexicalRetriever(corpus_chunks).search("", tenant_id="tenant-demo") == []

    def test_nonsense_query_returns_nothing_or_low_scores(self, corpus_chunks) -> None:
        """S7 依赖这一点：语料里没有的东西必须查不到，而不是返回最像的那个。"""
        results = LexicalRetriever(corpus_chunks).search(
            "zzzzz qqqqq wwwww", tenant_id="tenant-demo"
        )
        assert results == []

    def test_empty_corpus_is_safe(self) -> None:
        assert LexicalRetriever([]).search("anything", tenant_id="t") == []


class TestTokenize:
    def test_lowercases_and_splits(self) -> None:
        assert tokenize("Connection Pool EXHAUSTED") == ["connection", "pool", "exhausted"]

    def test_keeps_underscores_and_digits(self) -> None:
        assert tokenize("db_pool_active v1.5.0") == ["db_pool_active", "v1", "5", "0"]


# --------------------------------------------------------------------------
# 混合检索（用词法 + 一个假的第二检索器，避免测试依赖模型下载）
# --------------------------------------------------------------------------

class _StubRetriever:
    """返回指定 chunk 的假检索器。

    用它而不是真向量检索器：验证 RRF 的融合行为不该依赖 embedding 模型下载。
    构造时显式传入要返回的 chunk，使「两个检索器召回同一项」这个前提由测试控制，
    而不是碰运气。
    """

    name = "stub"

    def __init__(self, chunks: list[RunbookChunk]) -> None:
        self._chunks = chunks

    def search(self, query, *, tenant_id, service=None, top_k=5):
        return [
            ScoredChunk(chunk=c, score=1.0 - i * 0.1, sources=(self.name,))
            for i, c in enumerate(self._chunks[:top_k])
            if c.tenant_id == tenant_id
        ]


class TestHybridRetriever:
    @pytest.fixture(scope="class")
    def corpus_chunks(self) -> list[RunbookChunk]:
        return [c for b in load_runbooks(CORPUS) for c in b.chunks(embedding_model=MODEL)]

    def test_requires_at_least_one_retriever(self) -> None:
        with pytest.raises(ValueError):
            HybridRetriever([])

    def test_fusion_records_all_contributing_sources(self, corpus_chunks) -> None:
        lexical = LexicalRetriever(corpus_chunks)
        query = "connection pool exhausted"
        lexical_top = lexical.search(query, tenant_id="tenant-demo", top_k=1)
        assert lexical_top, "基线必须能召回，否则这条测试测不到融合"

        # 让 stub 返回词法检索器的首位结果，构造「两个检索器召回同一项」的场景。
        stub = _StubRetriever([lexical_top[0].chunk])
        hybrid = HybridRetriever([lexical, stub])
        results = hybrid.search(query, tenant_id="tenant-demo", top_k=10)
        assert results
        assert any(len(r.sources) > 1 for r in results), (
            "被两个检索器同时召回的项应记录两个来源，否则 RRF 没有累加"
        )

    def test_fusion_promotes_agreed_results(self, corpus_chunks) -> None:
        """RRF 的实际作用：两个检索器都召回的项应排在只有一个召回的项之前。"""
        lexical = LexicalRetriever(corpus_chunks)
        query = "connection pool exhausted"
        top_two = lexical.search(query, tenant_id="tenant-demo", top_k=2)
        assert len(top_two) == 2

        # stub 只认同第二名。融合后它应超过只被词法召回的第一名。
        stub = _StubRetriever([top_two[1].chunk])
        hybrid = HybridRetriever([lexical, stub])
        results = hybrid.search(query, tenant_id="tenant-demo", top_k=5)
        agreed = next(
            (r for r in results if r.chunk.point_id() == top_two[1].chunk.point_id()), None
        )
        assert agreed is not None
        assert len(agreed.sources) == 2

    def test_single_retriever_still_works(self, corpus_chunks) -> None:
        hybrid = HybridRetriever([LexicalRetriever(corpus_chunks)])
        assert hybrid.search("connection pool exhausted", tenant_id="tenant-demo")

    def test_cross_tenant_returns_nothing(self, corpus_chunks) -> None:
        hybrid = HybridRetriever([LexicalRetriever(corpus_chunks)])
        assert hybrid.search("connection pool", tenant_id="tenant-b") == []

    def test_low_relevance_results_are_filtered(self, corpus_chunks) -> None:
        """低相关结果不作为引用依据：静默返回它们会让 S7 的正确弃答无法判定。"""
        hybrid = HybridRetriever([LexicalRetriever(corpus_chunks)], min_relevance=0.99)
        results = hybrid.search("connection pool exhausted", tenant_id="tenant-demo", top_k=10)
        assert len(results) <= 2

    def test_action_intent_prefers_action_sections(self, corpus_chunks) -> None:
        """段落类型先验：问「怎么处置」时 safe_action/rollback 应更靠前。"""
        hybrid = HybridRetriever([LexicalRetriever(corpus_chunks)])
        diagnostic = hybrid.search(
            "what is causing the connection pool to saturate",
            tenant_id="tenant-demo",
            top_k=3,
        )
        remedial = hybrid.search(
            "how do I rollback the connection pool problem",
            tenant_id="tenant-demo",
            top_k=3,
        )
        assert diagnostic and remedial
        action_kinds = {SectionKind.SAFE_ACTION, SectionKind.ROLLBACK}
        remedial_action_count = sum(1 for r in remedial if r.chunk.section_kind in action_kinds)
        diagnostic_action_count = sum(1 for r in diagnostic if r.chunk.section_kind in action_kinds)
        assert remedial_action_count >= diagnostic_action_count

    def test_results_carry_citations(self, corpus_chunks) -> None:
        hybrid = HybridRetriever([LexicalRetriever(corpus_chunks)])
        result = hybrid.search("connection pool exhausted", tenant_id="tenant-demo", top_k=1)[0]
        citation = result.citation()
        assert set(citation) == {
            "document_id",
            "document_version",
            "section_id",
            "content_hash",
        }
        assert citation["content_hash"] == content_hash_of(result.chunk.content)
