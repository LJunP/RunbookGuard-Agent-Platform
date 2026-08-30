/**
 * 展示层纯函数的测试。
 *
 * 只测这些函数而不测组件渲染：组件的价值在于「操作者能不能看懂」，那需要人看
 * （M7 Gate 的条件是照 README 能复现演示流程，不是快照测试全绿）。
 * 而这些函数里藏着真正会出错的判断：AWAITING_APPROVAL 该是什么颜色、
 * 除零、摘要前缀。
 */

import { describe, expect, it } from "vitest";
import type { Run, TraceStep } from "../api/client";
import {
  budgetRows,
  budgetTone,
  decisionTone,
  formatInstant,
  formatMicros,
  shortDigest,
  statusTone,
  stepTone,
} from "./format";

function run(overrides: Partial<Run> = {}): Run {
  return {
    runId: "run-1",
    incidentId: "inc-1",
    graphVersion: "langgraph-v1",
    promptVersion: "p1",
    modelId: "fake-model",
    datasetVersion: "incidents-dev",
    status: "COMPLETE",
    currentStep: null,
    maxSteps: 25,
    stepsUsed: 5,
    deadline: "2026-08-29T14:00:00Z",
    costBudgetMicros: 500_000,
    costSpentMicros: 1_250,
    tokenBudget: 200_000,
    tokenSpent: 4_200,
    toolCallBudget: 20,
    toolCallCount: 3,
    failureClass: null,
    version: 1,
    ...overrides,
  };
}

function step(overrides: Partial<TraceStep> = {}): TraceStep {
  return {
    sequence: 1,
    nodeName: "EXECUTING_TOOL",
    status: "COMPLETED",
    toolCallId: null,
    failureClass: null,
    inputArtifact: null,
    outputArtifact: null,
    startedAt: "2026-08-29T13:00:00Z",
    finishedAt: null,
    ...overrides,
  };
}

describe("statusTone", () => {
  it("COMPLETE 是 ok", () => {
    expect(statusTone("COMPLETE")).toBe("ok");
  });

  it("FAILED 与 BLOCKED 是 danger", () => {
    expect(statusTone("FAILED")).toBe("danger");
    expect(statusTone("BLOCKED")).toBe("danger");
  });

  it("AWAITING_APPROVAL 是 warn 而不是 danger", () => {
    // 等人决策不是错误。显示成红色会诱导操作者去"修"一个本来正确的状态。
    expect(statusTone("AWAITING_APPROVAL")).toBe("warn");
  });

  it("未知状态退化为 muted，不抛异常", () => {
    expect(statusTone("SOME_FUTURE_STATE")).toBe("muted");
  });
});

describe("stepTone", () => {
  it("POLICY_CHECK 上的 failureClass 是 warn —— 那是成功的拦截", () => {
    expect(
      stepTone(step({ nodeName: "POLICY_CHECK", failureClass: "not_in_allowlist" })),
    ).toBe("warn");
  });

  it("其它节点上的 failureClass 是 danger", () => {
    expect(
      stepTone(step({ nodeName: "EXECUTING_TOOL", failureClass: "tool_timeout" })),
    ).toBe("danger");
  });

  it("没有 failureClass 时是 muted", () => {
    expect(stepTone(step())).toBe("muted");
  });
});

describe("decisionTone", () => {
  it("PENDING 是 warn，EXPIRED 是 muted", () => {
    expect(decisionTone("PENDING")).toBe("warn");
    expect(decisionTone("EXPIRED")).toBe("muted");
  });

  it("REJECTED 是 danger", () => {
    expect(decisionTone("REJECTED")).toBe("danger");
  });
});

describe("budgetRows", () => {
  it("四项预算都显示 已用/上限", () => {
    const rows = budgetRows(run());
    expect(rows.map((r) => r.label)).toEqual(["步数", "工具调用", "token", "成本"]);
    expect(rows[0]!.display).toBe("5 / 25");
  });

  it("上限为 0 时比例是 0，不是 NaN", () => {
    // 除零会让颜色判断变成 NaN 比较，结果是"永远不告警"。
    const rows = budgetRows(run({ toolCallBudget: 0, toolCallCount: 0 }));
    const toolRow = rows.find((r) => r.label === "工具调用");
    expect(toolRow?.ratio).toBe(0);
    expect(Number.isNaN(toolRow!.ratio)).toBe(false);
  });

  it("比例反映实际用量", () => {
    const rows = budgetRows(run({ stepsUsed: 20, maxSteps: 25 }));
    expect(rows[0]!.ratio).toBeCloseTo(0.8);
  });
});

describe("budgetTone", () => {
  it("接近上限时是 danger", () => {
    expect(budgetTone(0.95)).toBe("danger");
    expect(budgetTone(1.0)).toBe("danger");
  });

  it("0.7~0.9 是 warn", () => {
    expect(budgetTone(0.75)).toBe("warn");
  });

  it("低用量是 muted", () => {
    expect(budgetTone(0.1)).toBe("muted");
  });
});

describe("formatMicros", () => {
  it("0 就显示 0", () => {
    expect(formatMicros(0)).toBe("0");
  });

  it("极小额保留 micros 单位而不是显示 ¥0.0000", () => {
    // ¥0.0000 会让人以为免费。保留原始 micros 才能看出量级差异。
    expect(formatMicros(50)).toBe("50 μ");
  });

  it("可读额度换算成元", () => {
    expect(formatMicros(1_250_000)).toBe("¥1.2500");
  });
});

describe("shortDigest", () => {
  it("长摘要截断且保留原文前缀", () => {
    const digest = "a".repeat(64);
    const short = shortDigest(digest);
    expect(short).toBe("a".repeat(16) + "…");
    // 前缀必须是原文前缀，否则人无法用它核对"审批时与执行时是同一份参数"。
    expect(digest.startsWith(short.slice(0, 16))).toBe(true);
  });

  it("短字符串原样返回", () => {
    expect(shortDigest("abc")).toBe("abc");
  });
});

describe("formatInstant", () => {
  it("null 显示占位符", () => {
    expect(formatInstant(null)).toBe("—");
  });

  it("非法时间原样返回，不显示 Invalid Date", () => {
    expect(formatInstant("not-a-date")).toBe("not-a-date");
  });

  it("合法时间被格式化", () => {
    expect(formatInstant("2026-08-29T13:00:00Z")).not.toBe("2026-08-29T13:00:00Z");
  });
});
