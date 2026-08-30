"""检索服务：把语料、检索器与引用校验装配起来。

它是 retrieve_runbook_section 工具的实现后端，也是 M6 评测 harness 的入口。

单独一层的理由：工具层不该知道「有几个检索器」「用什么融合」，
而评测 harness 需要能替换检索器组合去对比。两者共用这一层就不会出现
「工具用 hybrid、评测用 lexical」这类口径不一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .metrics import CitationCheck, verify_citations
from .retrievers import (
    DEFAULT_TOP_K,
    Embedder,
    HybridRetriever,
    InMemoryVectorRetriever,
    LexicalRetriever,
    QdrantVectorRetriever,
    Retriever,
    ScoredChunk,
)
from .runbook import RunbookChunk, load_runbooks

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


@dataclass(frozen=True)
class RetrievalOutcome:
    """检索结果 + 是否命中。

    hit 与 sections 分开：空 sections 有两种含义——「查了但没有」与「没查」。
    S7 的正确弃答需要区分它们，因此 hit 是一个显式字段而不是 len(sections) > 0。
    """

    hit: bool
    sections: tuple[ScoredChunk, ...]
    retriever_name: str

    def citations(self) -> list[dict[str, str]]:
        return [s.citation() for s in self.sections]


class RetrievalService:
    def __init__(
        self,
        chunks: list[RunbookChunk],
        *,
        retriever: Retriever,
        embedding_model: str = DEFAULT_MODEL,
    ) -> None:
        self._chunks = list(chunks)
        self._retriever = retriever
        self.embedding_model = embedding_model
        # 引用校验索引。key 是 (section_id, document_version)：
        # 按 section_id 索引会让后加载的版本覆盖先前的，任何指向旧版本的引用都
        # 反查不到，而「旧 Run 的引用仍指向旧版本」是 UJ5 要求的性质。
        self._known: dict[tuple[str, str], str] = {
            (c.section_id, c.document_version): c.content_hash for c in self._chunks
        }
        self._sections: frozenset[str] = frozenset(c.section_id for c in self._chunks)
        self._by_version: dict[tuple[str, str], RunbookChunk] = {
            (c.section_id, c.document_version): c for c in self._chunks
        }

    # -- 工厂 -------------------------------------------------------------

    @classmethod
    def lexical_only(
        cls, corpus_dir: Path, *, embedding_model: str = DEFAULT_MODEL
    ) -> RetrievalService:
        """基线服务。不需要 embedding 模型，因此 CI 与快速测试用它。"""
        chunks = _load_chunks(corpus_dir, embedding_model)
        return cls(chunks, retriever=LexicalRetriever(chunks), embedding_model=embedding_model)

    @classmethod
    def hybrid_in_memory(
        cls, corpus_dir: Path, *, embedding_model: str = DEFAULT_MODEL
    ) -> RetrievalService:
        chunks = _load_chunks(corpus_dir, embedding_model)
        embedder = Embedder(embedding_model)
        vector = InMemoryVectorRetriever(chunks, embedder)
        vector.index()
        hybrid = HybridRetriever([LexicalRetriever(chunks), vector])
        return cls(chunks, retriever=hybrid, embedding_model=embedding_model)

    @classmethod
    def hybrid_qdrant(
        cls,
        corpus_dir: Path,
        *,
        url: str = "http://127.0.0.1:6333",
        embedding_model: str = DEFAULT_MODEL,
        index: bool = True,
    ) -> RetrievalService:
        chunks = _load_chunks(corpus_dir, embedding_model)
        embedder = Embedder(embedding_model)
        vector = QdrantVectorRetriever(chunks, embedder, url=url)
        if index:
            vector.index()
        hybrid = HybridRetriever([LexicalRetriever(chunks), vector])
        return cls(chunks, retriever=hybrid, embedding_model=embedding_model)

    # -- 检索 -------------------------------------------------------------

    def retrieve(
        self,
        symptom: str,
        *,
        tenant_id: str,
        service: str | None = None,
        top_k: int = DEFAULT_TOP_K,
    ) -> RetrievalOutcome:
        hits = self._retriever.search(
            symptom, tenant_id=tenant_id, service=service, top_k=top_k
        )
        return RetrievalOutcome(
            hit=bool(hits),
            sections=tuple(hits),
            retriever_name=self._retriever.name,
        )

    # -- 引用校验 ---------------------------------------------------------

    def verify(self, citations: list[dict[str, str]]) -> CitationCheck:
        return verify_citations(
            citations, known_chunks=self._known, known_sections=self._sections
        )

    def lookup(self, section_id: str, document_version: str) -> RunbookChunk | None:
        """按 section_id + version 反查原文。

        引用校验的另一半：不只验证「这条引用格式对」，还要能真的拿到它指向的内容。
        旧版本的引用必须仍能反查到旧内容（UJ5 的失败判据）。
        """
        return self._by_version.get((section_id, document_version))

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    @property
    def known_sections(self) -> dict[tuple[str, str], str]:
        """(section_id, document_version) -> content_hash。"""
        return dict(self._known)


def _load_chunks(corpus_dir: Path, embedding_model: str) -> list[RunbookChunk]:
    books = load_runbooks(corpus_dir)
    return [c for b in books for c in b.chunks(embedding_model=embedding_model)]
