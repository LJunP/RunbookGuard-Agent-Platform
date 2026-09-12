"""检索器。

三层，交付顺序即依赖顺序（ADR-0008 §2）：
  LexicalRetriever  —— BM25 基线，必须先有它的数字
  VectorRetriever   —— Qdrant + fastembed
  HybridRetriever   —— RRF 融合 + 规则 rerank

**tenant 过滤在检索之前**（ADR-0008 §7）：结果后过滤意味着向量检索已经跨租户召回过，
一旦漏一处就是泄漏。前置过滤让「忘记过滤」表现为「查不到东西」。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .runbook import RunbookChunk, SectionKind

RRF_K = 60
DEFAULT_TOP_K = 5
# 低于这个分数的结果不作为引用依据。S7 的正确弃答依赖它：
# 静默返回低相关结果会让「Runbook 缺失」变成「找到了但没用」。
MIN_RELEVANCE = 0.15

_TOKEN = re.compile(r"[a-z0-9_]+")

# 停用词。刻意只列虚词，不列任何领域词——把 "database" 之类的词列进来会让
# 「数据库慢」这类查询失去主要信号。
_STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those there here
    is are was were be been being am do does did doing done
    have has had having will would shall should can could may might must
    i you he she it we they me him her us them my your his its our their
    of in on at to for with from by about into over under between during
    as so such no not only very more most much many few
    what which who whom whose when where why how
    does do did done get gets got getting keep keeps kept
    """.split()
)

# 向量检索的余弦相似度下限。
#
# 实测的分布（36 篇语料、38 条真实查询、4 条期望未命中查询）：
#   真实查询 top-1：min 0.6995、p10 0.7408、median 0.8418
#   无关查询 top-1：0.5771 / 0.5898 / 0.6155 / 0.6981
#
# 两个分布**重叠**：最难的真实查询（0.6995）低于最像的无关查询（0.6981）只有
# 0.0014。这是小 embedding 模型在短查询上的固有局限，不是阈值没调好。
#
# 取 0.68：它挡住三条无关查询，放过全部真实查询。第四条（kernel-panic，0.6981）
# 挡不住——**这是已知残余风险，写进 M5 Gate 报告**。它由词法侧的实词覆盖率门槛
# 兜住：混合检索里两个检索器都要贡献排名，词法侧拒绝的项在 RRF 里只有一半权重。
MIN_VECTOR_SIMILARITY = 0.68

#
# 存在的理由：BM25 会给虚词打分，因此「quantum decoherence in the flux capacitor」
# 能靠 "in the" 命中一个完全无关的段落。归一化之后它的分数还是 1.0（因为它是
# 那批候选里最高的），任何分数阈值都挡不住。实词覆盖率是这类误命中的直接判据。
#
# 0.30 是实测扫描的结果（scripts/eval-retrieval.py 的 sweep）：
#   0.25 → Recall@5 0.9079，但 abstention 只有 0.750（一个无关查询仍命中）
#   0.30 → Recall@5 0.8553，abstention 1.000
#   0.34 → Recall@5 0.7895，abstention 1.000（召回白掉 5 个点，没有换到任何东西）
# 取能让 abstention 达到 1.000 的最低值。**S7 的正确弃答是安全属性，
# 不能用召回率去换**——一个会对不存在的故障编造 Runbook 引用的系统比召回低的系统更糟。
MIN_TERM_COVERAGE = 0.30


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def content_terms(text: str) -> set[str]:
    """去停用词后的实词集合。"""
    return {t for t in tokenize(text) if t not in _STOPWORDS and len(t) > 1}


@dataclass(frozen=True)
class ScoredChunk:
    chunk: RunbookChunk
    score: float
    # 来源标注：哪个检索器贡献了它。用于解释混合检索的结果，也用于调试。
    sources: tuple[str, ...] = ()

    def citation(self) -> dict[str, str]:
        return self.chunk.citation()


class Retriever(Protocol):
    name: str

    def search(
        self, query: str, *, tenant_id: str, service: str | None = None, top_k: int = DEFAULT_TOP_K
    ) -> list[ScoredChunk]:
        ...


# -- 词法基线 --------------------------------------------------------------


class LexicalRetriever:
    """BM25 基线。

    先于向量检索交付（说明书 §10 的硬要求）。它的数字是回答「embedding 带来了什么」
    的分母。
    """

    name = "lexical"

    def __init__(self, chunks: list[RunbookChunk], *, min_term_coverage: float = MIN_TERM_COVERAGE) -> None:
        from rank_bm25 import BM25Okapi

        self._chunks = list(chunks)
        self._corpus = [tokenize(f"{c.service} {c.content}") for c in self._chunks]
        self._terms = [content_terms(f"{c.service} {c.content}") for c in self._chunks]
        self._min_coverage = min_term_coverage
        # BM25Okapi 在空语料上会除零。
        self._bm25 = BM25Okapi(self._corpus) if self._corpus else None

    def search(
        self, query: str, *, tenant_id: str, service: str | None = None, top_k: int = DEFAULT_TOP_K
    ) -> list[ScoredChunk]:
        if self._bm25 is None:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        query_terms = content_terms(query)
        raw_scores = self._bm25.get_scores(tokens)

        # tenant 与 service 在打分**之后**用于筛选候选，但候选集本身是按 tenant 收窄的：
        # 这里遍历时跳过其它租户，等价于前置过滤。
        candidates: list[tuple[float, RunbookChunk]] = []
        for score, chunk, terms in zip(raw_scores, self._chunks, self._terms):
            if chunk.tenant_id != tenant_id:
                continue
            if service and chunk.service != service:
                continue
            if score <= 0:
                continue
            # 实词覆盖率门槛。BM25 会给虚词打分，因此完全无关的查询也能靠
            # "in the" 之类命中；归一化后它还是候选里的最高分，分数阈值挡不住。
            if query_terms:
                coverage = len(query_terms & terms) / len(query_terms)
                if coverage < self._min_coverage:
                    continue
            candidates.append((float(score), chunk))

        if not candidates:
            return []
        # BM25 分数无界，归一化到 [0,1] 只为让阈值可跨检索器比较；
        # 排名本身不受归一化影响。
        highest = max(score for score, _ in candidates)
        return [
            ScoredChunk(chunk=chunk, score=score / highest, sources=(self.name,))
            for score, chunk in sorted(candidates, key=lambda pair: -pair[0])[:top_k]
        ]


# -- 向量检索 --------------------------------------------------------------


class Embedder:
    """fastembed 包装。

    延迟加载模型：导入本模块不该触发几十 MB 的模型下载，否则任何只用词法检索的
    测试都要付这个代价。
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        self.model_name = model_name
        self._model: Any | None = None

    def _ensure(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure()
        return [vector.tolist() for vector in model.embed(texts)]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @property
    def dimension(self) -> int:
        # bge-small-en-v1.5 是 384 维。硬编而非探测：维度进 Qdrant 的集合定义，
        # 探测失败时静默用错的维度会让集合创建成功但检索永远为空。
        return 384


class InMemoryVectorRetriever:
    """内存向量检索。

    存在的理由：Qdrant 需要一个跑着的服务，而检索逻辑的正确性不该依赖它。
    这个实现与 QdrantVectorRetriever 有同一份契约测试，因此可以互换。
    """

    name = "vector"

    def __init__(
        self,
        chunks: list[RunbookChunk],
        embedder: Embedder,
        *,
        min_similarity: float = MIN_VECTOR_SIMILARITY,
    ) -> None:
        self._chunks = list(chunks)
        self._embedder = embedder
        self._min_similarity = min_similarity
        self._vectors: list[list[float]] = []

    def index(self) -> None:
        if not self._chunks:
            return
        texts = [f"{c.service}. {c.content}" for c in self._chunks]
        self._vectors = self._embedder.embed(texts)

    def search(
        self, query: str, *, tenant_id: str, service: str | None = None, top_k: int = DEFAULT_TOP_K
    ) -> list[ScoredChunk]:
        if not self._vectors:
            return []
        query_vector = self._embedder.embed_one(query)
        scored: list[tuple[float, RunbookChunk]] = []
        for vector, chunk in zip(self._vectors, self._chunks):
            if chunk.tenant_id != tenant_id:
                continue
            if service and chunk.service != service:
                continue
            similarity = _cosine(query_vector, vector)
            # 相似度门槛。向量检索总会返回「最像的那个」，即使查询与语料完全无关——
            # 没有下限就无法表达「检索未命中」，而 S7 的正确弃答依赖它。
            if similarity < self._min_similarity:
                continue
            scored.append((similarity, chunk))
        scored.sort(key=lambda pair: -pair[0])
        return [
            ScoredChunk(chunk=chunk, score=score, sources=(self.name,))
            for score, chunk in scored[:top_k]
        ]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class QdrantVectorRetriever:
    """Qdrant 向量检索。

    tenant 与 service 用 Qdrant 的 filter 前置过滤，不在结果后筛（威胁 T-4）。
    """

    name = "vector"
    COLLECTION = "runbook_sections"

    def __init__(
        self,
        chunks: list[RunbookChunk],
        embedder: Embedder,
        *,
        url: str = "http://127.0.0.1:6333",
        client: Any | None = None,
        min_similarity: float = MIN_VECTOR_SIMILARITY,
    ) -> None:
        self._chunks = {c.point_id(): c for c in chunks}
        self._embedder = embedder
        self._url = url
        self._client = client
        self._min_similarity = min_similarity

    def _ensure_client(self) -> Any:
        if self._client is None:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(url=self._url)
        return self._client

    def index(self, *, recreate: bool = True) -> None:
        from qdrant_client import models

        client = self._ensure_client()
        if recreate and client.collection_exists(self.COLLECTION):
            client.delete_collection(self.COLLECTION)
        if not client.collection_exists(self.COLLECTION):
            client.create_collection(
                collection_name=self.COLLECTION,
                vectors_config=models.VectorParams(
                    size=self._embedder.dimension, distance=models.Distance.COSINE
                ),
            )

        chunks = list(self._chunks.values())
        if not chunks:
            return
        vectors = self._embedder.embed([f"{c.service}. {c.content}" for c in chunks])
        client.upsert(
            collection_name=self.COLLECTION,
            points=[
                models.PointStruct(
                    id=chunk.point_id(),
                    vector=vector,
                    # payload 里存全部元数据：检索回来就能直接产出引用，
                    # 不需要再回查一次原文（那会引入一个可能不一致的来源）。
                    payload={
                        "document_id": chunk.document_id,
                        "document_version": chunk.document_version,
                        "section_id": chunk.section_id,
                        "section_kind": chunk.section_kind.value,
                        "service": chunk.service,
                        "tenant_id": chunk.tenant_id,
                        "content": chunk.content,
                        "content_hash": chunk.content_hash,
                        "chunker_version": chunk.chunker_version,
                        "embedding_model": chunk.embedding_model,
                        "indexed_at": chunk.indexed_at,
                    },
                )
                for chunk, vector in zip(chunks, vectors)
            ],
            wait=True,
        )

    def search(
        self, query: str, *, tenant_id: str, service: str | None = None, top_k: int = DEFAULT_TOP_K
    ) -> list[ScoredChunk]:
        from qdrant_client import models

        client = self._ensure_client()
        conditions = [
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))
        ]
        if service:
            conditions.append(
                models.FieldCondition(key="service", match=models.MatchValue(value=service))
            )

        hits = client.query_points(
            collection_name=self.COLLECTION,
            query=self._embedder.embed_one(query),
            query_filter=models.Filter(must=conditions),
            limit=top_k,
            # 门槛交给 Qdrant：它能在检索时就截断，比拉回来再过滤少传数据。
            score_threshold=self._min_similarity,
            with_payload=True,
        ).points

        out: list[ScoredChunk] = []
        for hit in hits:
            chunk = self._chunks.get(str(hit.id))
            if chunk is None:
                # payload 里有全部元数据，但这里仍要求本地有对应 chunk：
                # 索引里存在而本地不存在意味着索引比语料新，引用会指向反查不到的内容。
                continue
            out.append(ScoredChunk(chunk=chunk, score=float(hit.score), sources=(self.name,)))
        return out


# -- 混合检索 --------------------------------------------------------------

_DIAGNOSTIC_SECTIONS = frozenset({SectionKind.SYMPTOM, SectionKind.DIAGNOSIS})
_ACTION_SECTIONS = frozenset({SectionKind.SAFE_ACTION, SectionKind.ROLLBACK})
_ACTION_INTENT = frozenset(
    {"fix", "remediate", "mitigate", "action", "rollback", "restart", "throttle", "revert"}
)


class HybridRetriever:
    """RRF 融合 + 规则 rerank（ADR-0008 §3、§4）。

    rerank 是确定性规则而非模型：每一分都能说出来自哪条规则。
    **不得声称实现了神经 rerank。**
    """

    name = "hybrid"

    def __init__(
        self,
        retrievers: list[Retriever],
        *,
        rrf_k: int = RRF_K,
        min_relevance: float = MIN_RELEVANCE,
        require_all_retrievers: bool = True,
    ) -> None:
        if not retrievers:
            raise ValueError("hybrid retriever needs at least one sub-retriever")
        self._retrievers = retrievers
        self._rrf_k = rrf_k
        self._min_relevance = min_relevance
        self._require_all_retrievers = require_all_retrievers

    def search(
        self, query: str, *, tenant_id: str, service: str | None = None, top_k: int = DEFAULT_TOP_K
    ) -> list[ScoredChunk]:
        # 每个子检索器取更多候选：融合会重排，只取 top_k 会让第二个检索器的
        # 独特发现被提前截掉。
        fetch = max(top_k * 3, 10)
        fused: dict[str, dict[str, Any]] = {}
        contributing: set[str] = set()

        import time as _time

        from .. import observability as obs

        for retriever in self._retrievers:
            stage_started = _time.perf_counter()
            hits = retriever.search(query, tenant_id=tenant_id, service=service, top_k=fetch)
            # 按阶段计时：说明书 §16 的检索指标此前只有总延迟，出现问题时
            # 无法区分是 BM25 慢、向量查询慢还是融合慢。retriever.name 就是阶段名
            # （lexical / hybrid 的子检索器各有名字），不加新维度。
            obs.retrieval_stage_latency.labels(retriever.name).observe(
                _time.perf_counter() - stage_started
            )
            if hits:
                contributing.add(retriever.name)
            for rank, scored in enumerate(hits, start=1):
                key = scored.chunk.point_id()
                entry = fused.setdefault(
                    key, {"chunk": scored.chunk, "rrf": 0.0, "sources": []}
                )
                # RRF：只用排名，不受分数量纲影响（ADR-0008 §3）。
                entry["rrf"] += 1.0 / (self._rrf_k + rank)
                entry["sources"].append(retriever.name)

        if not fused:
            return []

        # 弃答的合成规则：**任一**子检索器判定未命中，混合结果也判未命中。
        #
        # 用「与」而非「或」的理由：弃答是安全属性。两个检索器各有各的盲区——
        # 词法侧挡不住同义改写，向量侧挡不住语义邻近但实际无关的查询（实测两个
        # 分布重叠，见 MIN_VECTOR_SIMILARITY 的注释）。要求两侧都认可，等于用
        # 各自的强项互相兜底。代价是召回：只有语义匹配而无词法重叠的真实查询
        # 会被误弃，这个代价在 M5 Gate 报告里如实记录。
        if self._require_all_retrievers and len(contributing) < len(self._retrievers):
            return []

        reranked = [
            (
                self._rerank_score(query, entry["chunk"], entry["rrf"]),
                entry["chunk"],
                tuple(entry["sources"]),
            )
            for entry in fused.values()
        ]
        reranked.sort(key=lambda triple: -triple[0])

        # 归一化后按阈值过滤：低相关结果不作为引用依据。
        highest = reranked[0][0] or 1.0
        out: list[ScoredChunk] = []
        for score, chunk, sources in reranked[:top_k]:
            normalized = score / highest
            if normalized < self._min_relevance:
                continue
            out.append(ScoredChunk(chunk=chunk, score=normalized, sources=sources))
        return out

    def _rerank_score(self, query: str, chunk: RunbookChunk, rrf: float) -> float:
        """可解释的 rerank。每一项都能说出理由。"""
        tokens = set(tokenize(query))
        if not tokens:
            return rrf

        content_tokens = set(tokenize(chunk.content))
        coverage = len(tokens & content_tokens) / len(tokens)

        # 服务名精确匹配：症状描述里的服务名是强信号。
        service_bonus = 0.25 if chunk.service.lower() in query.lower() else 0.0

        # 段落类型先验：问「这是什么故障」时 symptom/diagnosis 更相关，
        # 问「怎么处置」时 safe_action/rollback 更相关。
        wants_action = bool(tokens & _ACTION_INTENT)
        if wants_action:
            kind_bonus = 0.20 if chunk.section_kind in _ACTION_SECTIONS else 0.0
        else:
            kind_bonus = 0.20 if chunk.section_kind in _DIAGNOSTIC_SECTIONS else 0.0

        # rrf 放大 10 倍使其与覆盖度同量级——两者都在 [0,1] 附近才能相加。
        return rrf * 10.0 + coverage * 0.5 + service_bonus + kind_bonus
