"""检索接入工具层与引用端到端校验（M5）。

重点是引用的三项反查、版本隔离，以及「检索未命中」在工具层可观测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime.retrieval.service import RetrievalService
from agent_runtime.tools.contract import ResourceBinding, issue_authorization
from agent_runtime.tools.executor import ReadOnlyToolExecutor

CORPUS = Path(__file__).resolve().parents[3] / "datasets" / "runbooks"


@pytest.fixture(scope="module")
def service() -> RetrievalService:
    """词法基线服务。不用 hybrid：这些测试验证接线与引用，不验证检索质量，
    而 hybrid 需要下载 embedding 模型。"""
    return RetrievalService.lexical_only(CORPUS)


def _auth(symptom: str, *, tenant: str = "tenant-demo", service_name: str | None = None,
          top_k: int = 3):
    arguments = {"symptom": symptom, "top_k": top_k}
    if service_name:
        arguments["service"] = service_name
    return issue_authorization(
        tool_name="retrieve_runbook_section",
        binding=ResourceBinding(
            principal_id="prin-agent", tenant_id=tenant, resource_ref="runbook:*"
        ),
        arguments=arguments,
        arguments_digest="a" * 64,
        allowed=True,
        reason="test",
        requires_approval=False,
    )


class TestRetrievalWiring:
    async def test_hit_returns_sections_with_citations(self, service) -> None:
        executor = ReadOnlyToolExecutor("http://lab.test", retrieval=service)
        result = await executor.execute(
            _auth("db_pool_active reached db_pool_max and requests time out")
        )
        assert result.payload["retrieval_hit"] is True
        assert result.payload["sections"]
        first = result.payload["sections"][0]
        for field in ("document_id", "document_version", "section_id", "content_hash"):
            assert first[field], f"{field} must be present for citation verification"

    async def test_miss_is_observable(self, service) -> None:
        """S7 的正确弃答依赖这一点：未命中是可观测的事实，不是空数组。"""
        executor = ReadOnlyToolExecutor("http://lab.test", retrieval=service)
        result = await executor.execute(_auth("quantum decoherence in the flux capacitor"))
        assert result.payload["retrieval_hit"] is False
        assert result.payload["sections"] == []

    async def test_no_retrieval_configured_is_a_miss_not_an_error(self) -> None:
        """检索未接入是一种可观测状态，不是故障。"""
        executor = ReadOnlyToolExecutor("http://lab.test", retrieval=None)
        result = await executor.execute(_auth("connection pool exhausted"))
        assert result.payload["retrieval_hit"] is False

    async def test_sections_are_marked_untrusted(self, service) -> None:
        """Runbook 是版本化数据，不是系统指令（M0 §9）。"""
        executor = ReadOnlyToolExecutor("http://lab.test", retrieval=service)
        result = await executor.execute(_auth("connection pool exhausted after a release"))
        assert all(s["untrusted"] for s in result.payload["sections"])

    async def test_tenant_comes_from_binding_not_arguments(self, service) -> None:
        """tenant 从 binding 派生。工具参数里的 tenant 是不可信输入（威胁 T-4）。"""
        executor = ReadOnlyToolExecutor("http://lab.test", retrieval=service)
        # 另一个租户：语料全属 tenant-demo，因此必须查不到。
        result = await executor.execute(
            _auth("connection pool exhausted", tenant="tenant-b")
        )
        assert result.payload["retrieval_hit"] is False

    async def test_service_filter_is_honoured(self, service) -> None:
        executor = ReadOnlyToolExecutor("http://lab.test", retrieval=service)
        result = await executor.execute(
            _auth("queue depth keeps growing", service_name="synthetic-notify", top_k=5)
        )
        assert result.payload["retrieval_hit"] is True
        assert all(s["service"] == "synthetic-notify" for s in result.payload["sections"])

    async def test_top_k_is_respected(self, service) -> None:
        executor = ReadOnlyToolExecutor("http://lab.test", retrieval=service)
        result = await executor.execute(_auth("connection pool exhausted", top_k=2))
        assert len(result.payload["sections"]) <= 2

    async def test_result_is_json_serialisable(self, service) -> None:
        """结果要进 evidence 与 Trace，必须可序列化。"""
        executor = ReadOnlyToolExecutor("http://lab.test", retrieval=service)
        result = await executor.execute(_auth("connection pool exhausted"))
        json.dumps(result.payload)


class TestCitationVerificationEndToEnd:
    def test_retrieved_citations_all_verify(self, service) -> None:
        outcome = service.retrieve(
            "db_pool_active reached db_pool_max", tenant_id="tenant-demo", top_k=5
        )
        assert outcome.hit
        check = service.verify(outcome.citations())
        assert check.validity == 1.0, check.invalid_reasons

    def test_every_citation_can_be_looked_up(self, service) -> None:
        """引用校验的另一半：不只格式对，还要能真的拿到它指向的内容。"""
        outcome = service.retrieve("connection pool exhausted", tenant_id="tenant-demo", top_k=5)
        for citation in outcome.citations():
            chunk = service.lookup(citation["section_id"], citation["document_version"])
            assert chunk is not None, f"cannot resolve {citation}"
            assert chunk.content_hash == citation["content_hash"]

    def test_fabricated_citation_fails_verification(self, service) -> None:
        check = service.verify(
            [
                {
                    "document_id": "rb-made-up",
                    "document_version": "1.0.0",
                    "section_id": "rb-made-up#symptom",
                    "content_hash": "0" * 64,
                }
            ]
        )
        assert check.validity == 0.0

    def test_citation_with_wrong_version_fails(self, service) -> None:
        outcome = service.retrieve("connection pool exhausted", tenant_id="tenant-demo", top_k=1)
        citation = dict(outcome.citations()[0])
        citation["document_version"] = "99.0.0"
        assert service.verify([citation]).validity == 0.0

    def test_citation_with_tampered_hash_fails(self, service) -> None:
        outcome = service.retrieve("connection pool exhausted", tenant_id="tenant-demo", top_k=1)
        citation = dict(outcome.citations()[0])
        citation["content_hash"] = "f" * 64
        assert service.verify([citation]).validity == 0.0


class TestVersionIsolation:
    """UJ5 的失败判据：Runbook 更新后旧 Run 的引用仍必须指向旧版本。"""

    def test_both_versions_are_resolvable(self, service) -> None:
        old = service.lookup("rb-db-pool-exhaustion#diagnosis", "1.2.0")
        new = service.lookup("rb-db-pool-exhaustion#diagnosis", "2.0.0")
        assert old is not None, "旧版本必须仍可反查，否则历史引用失效"
        assert new is not None
        assert old.content != new.content
        assert old.content_hash != new.content_hash

    def test_old_citation_still_verifies_against_its_own_version(self, service) -> None:
        old = service.lookup("rb-db-pool-exhaustion#diagnosis", "1.2.0")
        assert old is not None
        chunk = service.lookup(old.section_id, old.document_version)
        assert chunk is not None
        assert chunk.content_hash == old.content_hash

    def test_versions_have_distinct_point_ids(self, service) -> None:
        old = service.lookup("rb-db-pool-exhaustion#diagnosis", "1.2.0")
        new = service.lookup("rb-db-pool-exhaustion#diagnosis", "2.0.0")
        assert old.point_id() != new.point_id()

    def test_unknown_version_is_not_resolved(self, service) -> None:
        assert service.lookup("rb-db-pool-exhaustion#diagnosis", "0.0.1") is None


class TestServiceFactories:
    def test_lexical_only_needs_no_model(self) -> None:
        svc = RetrievalService.lexical_only(CORPUS)
        assert svc.chunk_count > 0
        assert svc.retrieve("connection pool", tenant_id="tenant-demo").retriever_name == "lexical"

    def test_known_sections_cover_the_corpus(self, service) -> None:
        known = service.known_sections
        assert known
        for (section_id, version), content_hash in known.items():
            assert section_id and version
            assert len(content_hash) == 64

    def test_multi_version_sections_have_separate_entries(self, service) -> None:
        """按 section_id 索引会让版本互相覆盖，旧引用就反查不到了。"""
        known = service.known_sections
        target = "rb-db-pool-exhaustion#diagnosis"
        versions = {v for (s, v) in known if s == target}
        assert len(versions) >= 2, f"expected multiple versions of {target}, got {versions}"
