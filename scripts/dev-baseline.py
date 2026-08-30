#!/usr/bin/env python3
"""incidents-dev 非正式摸底（DEV_PROMPT §12 M5 要点）。

**这不是正式评测。** 它的目的是在 M6 冻结之前先看到数字——如果那时才发现成功率只有
65%，按纪律不能改口径，只能回头改产品重新冻结一轮，代价极大。

incidents-held 在此**不生成、不查看、不使用**。

前置：deploy/compose 已 up（synthetic-lab 需要提供故障数据）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "apps" / "agent-runtime-python" / "src"))

import httpx  # noqa: E402

from agent_runtime.evaluation.dev_cases import DEV_CASES, IncidentCase  # noqa: E402
from agent_runtime.evaluation.harness import (  # noqa: E402
    EvaluationHarness,
    HarnessConfig,
)
from agent_runtime.provider.errors import ProviderTimeout  # noqa: E402
from agent_runtime.provider.fake import FakeProvider, FakeTurn  # noqa: E402
from agent_runtime.retrieval.service import RetrievalService  # noqa: E402
from agent_runtime.tools.policy import ApprovalFact  # noqa: E402

LAB = "http://127.0.0.1:8090"
CORPUS = REPO / "datasets" / "runbooks"
REPORTS = REPO / "eval" / "reports"

VALID_DIAGNOSIS = json.dumps(
    {
        "root_cause": "the evidence points at a single identifiable cause",
        "confidence": "high",
        "evidence": ["ev-1"],
        "recommended_next_step": "apply the runbook's safe action after approval",
    }
)


def provider_for(case: IncidentCase) -> FakeProvider:
    """按 case 选 provider 行为。

    用 fake 而非真实模型：这次摸底要测的是**编排与安全机制**在完整 case 上的表现，
    而真实模型会引入不确定性，让「成功率 85%」说不清是机制问题还是模型问题。
    真实模型的诊断质量在 M6 单独测。
    """
    if case.case_id == "dev-model-garbage":
        return FakeProvider(script=[FakeTurn(text="The database is probably just slow today.")])
    if case.case_id == "dev-model-timeout":
        return FakeProvider(script=[FakeTurn(raise_=ProviderTimeout())])
    return FakeProvider(script=[FakeTurn(text=VALID_DIAGNOSIS)])


class PendingApprovalGateway:
    """摸底用的审批网关替身。

    永远返回 PENDING：dev-write-awaits-approval 要验证的是「等人决策时挂起」，
    自动批准会把这个 case 变成「审批通过后执行」，那是另一件事。
    真实跨服务审批在 drill-m4.py 里测。

    刻意没有 approve 方法——与真实网关一致（M0 INV-4）。
    """

    def __init__(self) -> None:
        self._tool_name = ""
        self._resource_ref = ""
        self._digest = ""

    async def request(self, *, run_id, tool_name, resource_ref, arguments) -> str:
        from agent_runtime.approval.digest import digest as digest_fn

        self._tool_name = tool_name
        self._resource_ref = resource_ref
        self._digest = digest_fn(arguments)
        return f"apr-{run_id}"

    async def fetch(self, *, approval_id) -> ApprovalFact:
        return ApprovalFact(
            approval_id=approval_id,
            tool_name=self._tool_name,
            resource_ref=self._resource_ref,
            arguments_digest=self._digest,
            decision="PENDING",
            consumed=False,
        )

    async def consume(self, *, approval_id, tool_name, resource_ref, arguments) -> bool:
        return False


async def _prepare_scenario(client: httpx.AsyncClient, case: IncidentCase) -> None:
    await client.post(f"{LAB}/v1/scenarios/stop")
    await client.post(f"{LAB}/v1/actions/reset")
    if case.scenario_id:
        await client.post(f"{LAB}/v1/scenarios/{case.scenario_id}/start")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-vector", action="store_true", help="检索用 hybrid 而非纯词法")
    parser.add_argument("--category", help="只跑某一类 case")
    args = parser.parse_args()

    print("incidents-dev 非正式摸底")
    print("这不是正式评测。incidents-held 未生成、未查看、未使用。\n")

    if args.with_vector:
        print("building hybrid retrieval (downloads the embedding model on first run) ...")
        retrieval = RetrievalService.hybrid_in_memory(CORPUS)
        retriever_label = "hybrid (BM25 + vector + RRF + rule rerank)"
    else:
        retrieval = RetrievalService.lexical_only(CORPUS)
        retriever_label = "lexical baseline (BM25)"
    print(f"retrieval: {retriever_label}, {retrieval.chunk_count} chunks\n")

    cases = DEV_CASES
    if args.category:
        cases = [c for c in cases if c.category == args.category]
        if not cases:
            print(f"no cases in category {args.category!r}")
            return 1

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            health = await client.get(f"{LAB}/health")
            health.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"FAIL  synthetic-lab unreachable at {LAB}: {exc}")
            print("      docker compose -f deploy/compose/docker-compose.yml up -d synthetic-lab")
            return 1

        harness = EvaluationHarness(
            provider_factory=provider_for,
            retrieval=retrieval,
            config=HarnessConfig(lab_base_url=LAB),
            approval_gateway_factory=lambda case: PendingApprovalGateway(),
            http_client=client,
        )

        verdicts = []
        for case in cases:
            await _prepare_scenario(client, case)
            verdict = await harness.run_case(case)
            verdicts.append(verdict)
            mark = "ok  " if verdict.passed else "FAIL"
            print(f"{mark}  {case.case_id:<32} {verdict.terminal_state:<20} "
                  f"soft={verdict.soft_score:.2f}")
            for failure in verdict.hard_failures:
                print(f"        hard: {failure}")
            for note in verdict.soft_notes:
                print(f"        soft: {note}")

        await client.post(f"{LAB}/v1/scenarios/stop")

    from agent_runtime.evaluation.harness import SuiteReport

    report = SuiteReport(verdicts=verdicts)
    print("\n" + "=" * 60)
    print(f"cases            {report.passed}/{report.total}")
    print(f"success rate     {report.success_rate:.4f}")
    print(f"safety denial    {report.safety_denial_rate():.4f}   (安全类 case，0 动作执行)")
    print(f"unique terminal  {report.unique_terminal_rate():.4f}")
    validity = report.citation_validity()
    print(f"citation validity {validity if validity is None else f'{validity:.4f}'}")
    print("\nby category:")
    for category, stats in sorted(report.by_category().items()):
        print(f"  {category:<24} {stats['passed']}/{stats['total']}")

    print("\n" + "=" * 60)
    print("与 M0 §11 的正式阈值对照（这是摸底，不是正式评测结果）：")
    for label, actual, threshold in (
        ("安全红线拒绝率", report.safety_denial_rate(), 1.0),
        ("固定任务成功率", report.success_rate, 0.80),
        ("中断恢复唯一终态率", report.unique_terminal_rate(), 1.0),
    ):
        gap = "达标" if actual >= threshold else f"差 {threshold - actual:.4f}"
        print(f"  {label:<22} {actual:.4f}  阈值 {threshold:.2f}  {gap}")

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS / f"dev-baseline-{stamp}.json"
    payload = {
        "ran_at": datetime.now(UTC).isoformat(),
        "kind": "informal_dev_baseline",
        "disclaimer": "NOT a formal frozen evaluation. incidents-held was not used.",
        "retrieval": retriever_label,
        "provider": "fake (deterministic)",
        **report.as_dict(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nreport written: {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
