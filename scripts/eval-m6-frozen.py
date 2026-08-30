#!/usr/bin/env python3
"""M6 正式冻结评测（DEV_PROMPT §11 + §12 M6）。

**纪律，写在最前面：**

  1. **只跑一次。** 不允许反复运行到 PASS。若阈值未达标，不能改判据口径——
     只能回头改产品，重新冻结，再跑一次，并在报告里写明这是第几轮冻结。
  2. **评测不修改产品代码。** 这个脚本只读产品代码，不给它打补丁、不注入桩。
  3. **incidents-held 在此第一次也是唯一一次使用。** 它的内容在开发期未被查看。
  4. 已有报告存在时**拒绝覆盖**：覆盖等于销毁上一轮的证据。

前置：
  docker compose -f deploy/compose/docker-compose.yml up -d synthetic-lab qdrant

用法：
  python3 scripts/eval-m6-frozen.py                    # 词法基线检索
  python3 scripts/eval-m6-frozen.py --with-vector      # hybrid 检索
  python3 scripts/eval-m6-frozen.py --round 2 --reason "..."   # 第二轮冻结
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

from agent_runtime.evaluation.dev_cases import DEV_CASES, PROBE_CASES  # noqa: E402
from agent_runtime.evaluation.freeze import (  # noqa: E402
    build_manifest,
    check_thresholds,
)
from agent_runtime.evaluation.harness import (  # noqa: E402
    EvaluationHarness,
    HarnessConfig,
    SuiteReport,
)
from agent_runtime.evaluation.held_cases import (  # noqa: E402
    HeldDatasetError,
    dataset_digest,
    load_held_cases,
)
from agent_runtime.evaluation.trace import compare  # noqa: E402
from agent_runtime.retrieval.service import RetrievalService  # noqa: E402

LAB = "http://127.0.0.1:8090"
CORPUS = REPO / "datasets" / "runbooks"
REPORTS = REPO / "eval" / "reports"
TRACES = REPO / "eval" / "traces"


def frozen_t0() -> str:
    """冻结的观测窗口起点，对齐到整分钟。

    为什么必须冻结：synthetic-lab 的 T0 对齐到整分钟，但**绝对**分钟随启动时刻
    变化。指标载荷带绝对时间戳，而 evidence_id 是载荷的内容摘要，
    因此跨过一分钟边界的两次运行会得到不同的 evidence_id ——
    行为完全相同而 Trace 摘要不同。

    第 3 轮冻结评测就是这样暴露的：第 1、2 轮各跑 17 秒恰好落在同一分钟，
    Replay 报「一致」；第 3 轮跨过 03:08:00，37 个 case 的证据 id 全变。
    **前两轮的「一致」是运气，不是系统确定性。**

    修法是把观测窗口变成冻结配置的一项，而不是把时间戳从摘要里排除掉——
    后者会让「证据内容真的变了」也被当成一致。

    用 Z 后缀而不是 +00:00：查询串里的 `+` 按 HTTP 规范解码成空格。
    """
    return datetime.now(UTC).replace(second=0, microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


async def _prepare_scenario(client: httpx.AsyncClient, case, t0: str) -> None:
    await client.post(f"{LAB}/v1/scenarios/stop")
    await client.post(f"{LAB}/v1/actions/reset")
    if case.scenario_id:
        response = await client.post(
            f"{LAB}/v1/scenarios/{case.scenario_id}/start", params={"t0": t0}
        )
        if response.status_code != 200:
            # 冻结窗口没设上就中止：继续跑会得到一个可复现性未知的结果，
            # 而那个结果看起来与正常的一样。
            raise RuntimeError(
                f"could not start {case.scenario_id} with frozen t0={t0}: "
                f"HTTP {response.status_code} {response.text[:200]}"
            )


async def _run_suite(
    harness: EvaluationHarness,
    client: httpx.AsyncClient,
    cases: list,
    label: str,
    t0: str,
) -> SuiteReport:
    print(f"\n== {label} ({len(cases)} cases) ==")
    verdicts = []
    for case in cases:
        await _prepare_scenario(client, case, t0)
        verdict = await harness.run_case(case)
        verdicts.append(verdict)
        expected_fail = case.expects_failure
        ok = verdict.passed is not expected_fail
        mark = "ok  " if ok else "MISS"
        suffix = "  (expected to fail)" if expected_fail else ""
        print(
            f"{mark}  {case.case_id:<40} {verdict.terminal_state:<20} "
            f"soft={verdict.soft_score:.2f}{suffix}"
        )
        if not ok:
            for failure in verdict.hard_failures:
                print(f"        hard: {failure}")
    await client.post(f"{LAB}/v1/scenarios/stop")
    return SuiteReport(
        verdicts=verdicts,
        expects_failure={c.case_id: c.expects_failure for c in cases},
    )


def _print_metrics(report: SuiteReport, label: str) -> None:
    print(f"\n-- {label} --")
    print(f"cases matching expectation  {report.passed}/{report.total}")
    print(f"success rate               {report.success_rate:.4f}")
    for name, value in (
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
        ("violation detection", report.violation_detection_rate()),
    ):
        shown = "n/a (no sample)" if value is None else f"{value:.4f}"
        print(f"{name:<26} {shown}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-vector", action="store_true", help="检索用 hybrid 而非纯词法")
    parser.add_argument("--round", type=int, default=1, help="这是第几轮冻结评测")
    parser.add_argument("--reason", default="", help="第 2 轮及以后必须说明重新冻结的原因")
    parser.add_argument(
        "--skip-held",
        action="store_true",
        help="只跑 dev+probe。用于验证脚本本身可运行——正式评测不许加这个参数",
    )
    args = parser.parse_args()

    if args.round > 1 and not args.reason:
        print("FAIL  第 2 轮及以后必须用 --reason 说明为什么重新冻结。")
        print("      按 DEV_PROMPT §11，重新冻结只能因为改了产品，不能因为想要更好的数字。")
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # dry run 写到另一个文件名，且不检查已有报告：否则一次「验证脚本能跑」
    # 就会占掉第 1 轮正式评测的名额，逼着人用 --round 2 起步。
    if args.skip_held:
        report_path = REPORTS / f"m6-dryrun-{stamp}.json"
    else:
        report_path = REPORTS / f"m6-frozen-round{args.round}-{stamp}.json"
        existing = sorted(REPORTS.glob(f"m6-frozen-round{args.round}-*.json"))
        if existing:
            print(f"REFUSED  第 {args.round} 轮的正式评测报告已存在：")
            for path in existing:
                print(f"         {path.name}")
            print("         覆盖它等于销毁上一轮的证据。要重跑必须用新的 --round 并给出 --reason。")
            return 1

    print("=" * 72)
    if args.skip_held:
        print("M6 评测 —— DRY RUN（--skip-held）")
        print("这不是正式评测：held 未使用，结果不得引用为 Gate 依据。")
    else:
        print("M6 正式冻结评测")
        print("这一次运行的结果就是最终结果。不会反复运行到 PASS。")
    print(f"轮次 {args.round}" + (f"    原因：{args.reason}" if args.reason else ""))
    print("=" * 72)

    if args.with_vector:
        print("\nbuilding hybrid retrieval (first run downloads the embedding model) ...")
        retrieval = RetrievalService.hybrid_in_memory(CORPUS)
        retriever = "hybrid (BM25 + vector + RRF + rule rerank)"
    else:
        retrieval = RetrievalService.lexical_only(CORPUS)
        retriever = "lexical baseline (BM25)"
    print(f"retrieval: {retriever}, {retrieval.chunk_count} chunks")

    held_cases: list = []
    held_digest = "NOT_USED"
    if not args.skip_held:
        try:
            held_cases = load_held_cases()
            held_digest = dataset_digest()
        except HeldDatasetError as exc:
            print(f"\nFAIL  incidents-held 不可用：{exc}")
            print("      python3 scripts/generate-incidents-held.py")
            return 1
        print(f"incidents-held: {len(held_cases)} cases, digest {held_digest[:16]}")

    t0 = frozen_t0()
    print(f"frozen observation window t0: {t0}")

    manifest = build_manifest(
        dataset="incidents-dev + incidents-held",
        model_id="scripted-diagnosis + fake-model",
        provider_id="scripted/fake (deterministic)",
        retriever=retriever,
        held_digest=held_digest,
        observation_window_t0=t0,
    )
    print(f"\nfreeze fingerprint: {manifest.fingerprint()[:16]}")
    print(f"candidate commit:   {manifest.candidate_commit[:12]}")
    if not manifest.working_tree_clean:
        # 不中止：中止会让「先提交再评测」变成硬要求，而这一点应由人决定。
        # 但事实必须记进报告——否则「这个 commit 的代码」这句话不成立。
        print("WARN  工作区不干净。这个事实已记入冻结清单与报告。")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            (await client.get(f"{LAB}/health")).raise_for_status()
        except httpx.HTTPError as exc:
            print(f"\nFAIL  synthetic-lab unreachable at {LAB}: {exc}")
            print("      docker compose -f deploy/compose/docker-compose.yml up -d synthetic-lab")
            return 1

        harness = EvaluationHarness(
            retrieval=retrieval,
            config=HarnessConfig(lab_base_url=LAB),
            http_client=client,
            config_fingerprint=manifest.fingerprint(),
        )

        dev_report = await _run_suite(harness, client, DEV_CASES, "incidents-dev", t0)
        dev_traces = dict(harness.traces)

        probe_report = await _run_suite(
            harness, client, PROBE_CASES, "grader probes", t0
        )

        held_report: SuiteReport | None = None
        if held_cases:
            held_report = await _run_suite(
                harness, client, held_cases, "incidents-held", t0
            )

        # Replay：dev 全套再跑一遍，比对 trace。
        # 同一配置的两次运行行为不同，则「跑一次」的结论不成立。
        print("\n== replay (incidents-dev) ==")
        replay_harness = EvaluationHarness(
            retrieval=retrieval,
            config=HarnessConfig(lab_base_url=LAB),
            http_client=client,
            config_fingerprint=manifest.fingerprint(),
        )
        for case in DEV_CASES:
            # 同一个 t0：Replay 要验证的是「同一配置的两次运行行为是否相同」，
            # 而观测窗口是配置的一部分。传不同的 t0 等于换了输入再问行为为什么不同。
            await _prepare_scenario(client, case, t0)
            await replay_harness.run_case(case)
        await client.post(f"{LAB}/v1/scenarios/stop")

        replay_diffs = []
        for case_id, trace in dev_traces.items():
            diff = compare(trace, replay_harness.traces[case_id])
            if not diff.identical:
                replay_diffs.append(diff)
        if replay_diffs:
            print(f"MISS  {len(replay_diffs)} 个 case 的重跑行为与首跑不一致：")
            for diff in replay_diffs:
                print(f"      {diff.case_id}: {list(diff.differences)[:2]}")
        else:
            print(f"ok    {len(dev_traces)} 个 case 的重跑行为与首跑逐字节一致")

    _print_metrics(dev_report, "incidents-dev")
    _print_metrics(probe_report, "grader probes")
    if held_report is not None:
        _print_metrics(held_report, "incidents-held")

    # 阈值判定以 held 为准（若跑了）：dev 在开发期被反复看过，
    # 用它对照阈值等于用练习题的成绩当考试成绩。
    gate_report = held_report or dev_report
    gate_label = "incidents-held" if held_report is not None else "incidents-dev"
    checks = check_thresholds(gate_report)

    print("\n" + "=" * 72)
    print(f"M0 §11 阈值判定（依据：{gate_label}）")
    print("=" * 72)
    all_met = True
    for check in checks:
        actual = "n/a" if check.actual is None else f"{check.actual:.4f}"
        status = "达标" if check.met else "未达标"
        print(f"  {check.metric:<28} {actual:>8}  阈值 {check.threshold:.2f}  {status}")
        if check.note:
            print(f"      {check.note}")
        all_met = all_met and check.met

    replay_ok = not replay_diffs
    print(f"\n  replay determinism{'':<12} {'一致' if replay_ok else '不一致'}")
    gate_passed = all_met and replay_ok

    print("\n" + "=" * 72)
    if gate_passed:
        print("M6 Gate：全部阈值达标，且重跑行为一致。")
        print("按 DEV_PROMPT §12，此时才允许把这个项目写进简历，数字必须原样引用。")
    else:
        print("M6 Gate：未通过。")
        print("按 DEV_PROMPT §11，此时**不能**修改判据口径或阈值定义。")
        print("只能回头改产品，重新冻结，用 --round 2 --reason 再跑一轮。")
    print("=" * 72)

    TRACES.mkdir(parents=True, exist_ok=True)
    # dry run 的目录名带 dryrun 前缀。都叫 round{N} 会让 eval/traces/ 里出现
    # 两个 "round1"，而其中一个不是正式评测的证据——看目录名分不出来。
    label = f"dryrun-{stamp}" if args.skip_held else f"round{args.round}-{stamp}"
    trace_dir = TRACES / label
    for case_id, trace in dev_traces.items():
        trace.write(trace_dir / f"{case_id}.json")

    payload = {
        "kind": "dry_run_not_formal" if args.skip_held else "formal_frozen_evaluation",
        "round": args.round,
        "reason": args.reason or None,
        "ran_at": datetime.now(UTC).isoformat(),
        "gate_passed": gate_passed,
        "gate_basis": gate_label,
        "discipline": (
            "Run exactly once. Thresholds and grader definitions were fixed before "
            "this run and are recorded in the freeze manifest."
        ),
        "freeze_manifest": manifest.as_dict(),
        "threshold_checks": [c.as_dict() for c in checks],
        "replay": {
            "cases_replayed": len(dev_traces),
            "identical": replay_ok,
            "diffs": [d.as_dict() for d in replay_diffs],
        },
        "suites": {
            "incidents_dev": dev_report.as_dict(),
            "grader_probes": probe_report.as_dict(),
            "incidents_held": held_report.as_dict() if held_report else None,
        },
        "traces_dir": str(trace_dir.relative_to(REPO)),
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nreport: {report_path.relative_to(REPO)}")
    print(f"traces: {trace_dir.relative_to(REPO)}")
    return 0 if gate_passed else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
