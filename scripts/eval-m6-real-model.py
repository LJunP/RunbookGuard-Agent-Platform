#!/usr/bin/env python3
"""M6 的真实模型诊断质量测量（与冻结评测分开）。

**为什么分开跑：** 冻结评测用脚本化 provider，量的是**编排与安全机制**——
真实模型的不确定性会让「成功率 100%」说不清是机制对还是模型好。这个脚本反过来：
机制不变，换成真实模型，量的是**诊断质量**。

**这不是冻结评测的一部分。** 它的数字单独报告，不参与 M0 §11 的阈值判定，
因为它不可复现（同一 prompt 的真实模型输出可能不同）。混进去会让整个
冻结评测失去「跑一次即可」的正当性。

受控（与 M3 的真实模型验证同一套纪律）：
  - 凭据只从环境变量读，绝不写入任何文件
  - 硬调用上限与成本上限，超限即停
  - 落盘 artifact 前全部过 redact()
  - 默认只跑一小批 case，需要显式 --cases 扩大

用法：
    RUNBOOKGUARD_LLM_BASE_URL=... \
    RUNBOOKGUARD_LLM_MODEL=... \
    RUNBOOKGUARD_LLM_API_KEY=... \
    python3 scripts/eval-m6-real-model.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "apps" / "agent-runtime-python" / "src"))

import httpx  # noqa: E402

from agent_runtime.evaluation.dev_cases import BY_ID  # noqa: E402
from agent_runtime.evaluation.harness import (  # noqa: E402
    EvaluationHarness,
    HarnessConfig,
    SuiteReport,
)
from agent_runtime.provider.models import ProviderConfig  # noqa: E402
from agent_runtime.provider.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_runtime.provider.pricing import lookup as lookup_pricing  # noqa: E402
from agent_runtime.redaction import redact  # noqa: E402
from agent_runtime.retrieval.service import RetrievalService  # noqa: E402

LAB = os.environ.get("SYNTHETIC_LAB_URL", "http://127.0.0.1:8090")
CORPUS = REPO / "datasets" / "runbooks"
REPORTS = REPO / "eval" / "reports"

# 默认这一批：覆盖正向诊断、归因陷阱、正确弃答、矛盾声明、注入抵抗。
# 不跑有界执行与 provider 失败类——那些与模型质量无关，白花预算。
DEFAULT_CASES = (
    "dev-pool-exhaustion",
    "dev-mq-backlog",
    "dev-redis-hot-key",
    "dev-config-401",
    "dev-config-401-downstream-is-healthy",
    "dev-readiness-failure",
    "dev-oomkilled",
    "dev-retry-storm",
    "dev-retry-storm-downstream-is-victim",
    "dev-runbook-missing",
    "dev-conflicting-signals",
    "dev-injection-no-escalation",
)

# 一次运行的硬上限。一个循环 bug 不该把预算烧光。
#
# 第一轮实测：18 次调用花掉 76778 micros（按 list price，约 0.077 元），
# 几乎顶到当时的 80000 上限。证据摘要接入后 prompt 更长，因此上调。
MAX_CALLS = 40
MAX_COST_MICROS = 300_000  # ≈ 0.30 元 list price（当日 5 折实付约 0.15 元）


def _system_prompt_note() -> str:
    """真实模型需要知道 schema，否则它不可能猜到 claims / conflicting_signals 的形状。

    这不是「帮模型作弊」：ADR-0009 的判据校验的是**引用的证据是否真实存在**、
    **归因是否正确**，告诉它字段名不会让这些变得更容易。反过来，不给 schema
    等于在测「模型能否猜中我的字段名」，那不是诊断能力。
    """
    from agent_runtime.schemas import Diagnosis

    return json.dumps(Diagnosis.model_json_schema(), ensure_ascii=False)


class SchemaAwareProvider:
    """在 prompt 里补上 Diagnosis 的 JSON Schema，然后转交真实 Provider。

    产品 prompt（`_build_prompt`）已经列出了必填字段名、conclusion_type 的取值、
    以及「只能引用列出的证据 id」这些规则。这里**只**补 JSON Schema，
    因为嵌套结构（claims 里每项的形状、conflicting_signals 的三个键）用文字说不清。

    包装而不是改 `_build_prompt`：冻结评测用的是同一个 `_build_prompt`，
    在这里改它会让两条路径的 prompt 不同，从而使冻结清单里记录的指纹对不上。
    """

    def __init__(self, inner: OpenAICompatibleProvider) -> None:
        self._inner = inner
        self.provider_id = inner.provider_id
        self.model = inner.config.model
        self._schema = _system_prompt_note()

    @property
    def calls_made(self) -> int:
        return self._inner.calls_made

    @property
    def cost_micros_spent(self) -> int:
        return self._inner.cost_micros_spent

    async def complete(self, messages):
        from agent_runtime.provider.models import ChatMessage

        augmented = list(messages)
        augmented.insert(
            1,
            ChatMessage(
                role="system",
                content=f"JSON Schema of the object to return: {self._schema}",
            ),
        )
        return await self._inner.complete(augmented)

    async def stream(self, messages):
        async for item in self._inner.stream(messages):
            yield item


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", nargs="*", default=list(DEFAULT_CASES))
    parser.add_argument("--with-vector", action="store_true")
    args = parser.parse_args()

    missing = [
        name
        for name in (
            "RUNBOOKGUARD_LLM_BASE_URL",
            "RUNBOOKGUARD_LLM_MODEL",
            "RUNBOOKGUARD_LLM_API_KEY",
        )
        if not os.environ.get(name)
    ]
    if missing:
        print("FAIL  这些环境变量未设置：" + ", ".join(missing))
        print("      凭据只从环境变量读，不从文件读。")
        return 1

    unknown = [cid for cid in args.cases if cid not in BY_ID]
    if unknown:
        print(f"FAIL  未知 case: {unknown}")
        return 1
    cases = [BY_ID[cid] for cid in args.cases]

    print("=" * 72)
    print("M6 真实模型诊断质量测量")
    print("这不是冻结评测：结果不参与 M0 §11 的阈值判定，因为它不可复现。")
    print("=" * 72)

    model_id = os.environ["RUNBOOKGUARD_LLM_MODEL"]
    pricing = lookup_pricing(model_id)
    if pricing is None:
        # fail-closed（M8 §6 头号缺口的整改）：没有计价就配成本预算，预算会
        # 静默失效。显式拒绝，不 warn-and-continue。
        print(f"FAIL  {model_id} 不在计价表里。把它加进 provider/pricing.py，")
        print(
            "      或设 RUNBOOKGUARD_ALLOW_UNKNOWN_PRICING=1 显式承认成本预算不可执行。"
        )
        return 1
    config = ProviderConfig(
        base_url=os.environ["RUNBOOKGUARD_LLM_BASE_URL"],
        model=model_id,
        api_key=os.environ["RUNBOOKGUARD_LLM_API_KEY"],
        max_calls=MAX_CALLS,
        max_cost_micros=MAX_COST_MICROS,
        price_per_1k_prompt_micros=pricing.prompt_micros_per_1k,
        price_per_1k_completion_micros=pricing.completion_micros_per_1k,
        timeout_seconds=90.0,
    )
    print(f"provider: {config!r}")
    print(f"budget:   max_calls={MAX_CALLS} max_cost={MAX_COST_MICROS} micros")

    if args.with_vector:
        retrieval = RetrievalService.hybrid_in_memory(CORPUS)
        retriever = "hybrid (BM25 + vector + RRF + rule rerank)"
    else:
        retrieval = RetrievalService.lexical_only(CORPUS)
        retriever = "lexical baseline (BM25)"
    print(f"retrieval: {retriever}, {retrieval.chunk_count} chunks\n")

    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            (await client.get(f"{LAB}/health")).raise_for_status()
        except httpx.HTTPError as exc:
            print(f"FAIL  synthetic-lab unreachable at {LAB}: {exc}")
            return 1

        # 一个 provider 实例贯穿全部 case：调用与成本的上限必须是**整轮**的上限，
        # 每个 case 各自一份上限等于没有上限。
        inner = OpenAICompatibleProvider(config, client=client)
        provider = SchemaAwareProvider(inner)

        harness = EvaluationHarness(
            provider_factory=lambda case: provider,
            retrieval=retrieval,
            config=HarnessConfig(lab_base_url=LAB),
            http_client=client,
        )

        verdicts = []
        errors: list[dict] = []
        for case in cases:
            await client.post(f"{LAB}/v1/scenarios/stop")
            await client.post(f"{LAB}/v1/actions/reset")
            if case.scenario_id:
                await client.post(f"{LAB}/v1/scenarios/{case.scenario_id}/start")
            try:
                verdict = await harness.run_case(case)
            except Exception as exc:  # noqa: BLE001
                # 记下来继续：一个 case 触发的 Provider 异常不该终止整轮测量，
                # 但必须出现在报告里而不是被吞掉。
                errors.append({"case_id": case.case_id, "error": redact(str(exc))})
                print(f"ERR   {case.case_id:<40} {redact(str(exc))[:80]}")
                continue
            verdicts.append(verdict)
            mark = "ok  " if verdict.passed else "FAIL"
            print(f"{mark}  {case.case_id:<40} {verdict.terminal_state:<20} "
                  f"conclusion={verdict.conclusion_type}")
            for failure in verdict.hard_failures:
                print(f"        {redact(failure)}")
            if verdict.failure_class:
                # 失败原因的细节在 step detail 里。不打印它等于只知道
                # 「provider_failure」而不知道是超时、垃圾输出还是 schema 不符。
                trace = harness.traces.get(case.case_id)
                for step in (trace.steps if trace else []):
                    if step.failure_code:
                        print(f"        detail: {redact(step.detail)[:300]}")

        await client.post(f"{LAB}/v1/scenarios/stop")

    report = SuiteReport(
        verdicts=verdicts,
        expects_failure={c.case_id: c.expects_failure for c in cases},
    )
    print("\n" + "=" * 72)
    print("真实模型上的诊断质量（不是 Gate 依据）")
    print("=" * 72)
    print(f"cases run                  {len(verdicts)}/{len(cases)}"
          + (f"  ({len(errors)} errored)" if errors else ""))
    print(f"cases matching expectation {report.passed}/{report.total}")
    for name, value in (
        ("success rate", report.success_rate),
        ("safety denial rate", report.safety_denial_rate()),
        ("tool schema validity", report.tool_schema_validity()),
        ("unique terminal rate", report.unique_terminal_rate()),
        ("diagnosis schema validity", report.schema_validity()),
    ):
        print(f"{name:<26} {value:.4f}")
    for name, value in (
        ("citation validity", report.citation_validity()),
        ("answer groundedness", report.answer_groundedness()),
        ("attribution accuracy", report.attribution_accuracy()),
        ("abstention rate", report.abstention_rate()),
    ):
        shown = "n/a (no sample)" if value is None else f"{value:.4f}"
        print(f"{name:<26} {shown}")
    print(f"\nprovider calls             {provider.calls_made}")
    print(f"cost (micros, list price)  {provider.cost_micros_spent}")

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS / f"m6-real-model-{stamp}.json"
    payload = {
        "kind": "real_model_quality_measurement",
        "not_a_frozen_evaluation": (
            "Real model output is not reproducible; these numbers do not participate "
            "in the M0 §11 threshold decision."
        ),
        "ran_at": datetime.now(UTC).isoformat(),
        "model": config.model,
        "provider_id": config.provider_id,
        "retriever": retriever,
        "budget": {"max_calls": MAX_CALLS, "max_cost_micros": MAX_COST_MICROS},
        "usage": {
            "calls": provider.calls_made,
            "cost_micros_list_price": provider.cost_micros_spent,
        },
        "errors": errors,
        "report": report.as_dict(),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nreport: {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
