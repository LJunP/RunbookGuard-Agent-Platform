/** 展示层的纯函数。放在组件外面，因为它们是这里唯一值得单测的东西。 */

import type { Run, TraceStep } from "../api/client";

export type Tone = "ok" | "warn" | "danger" | "muted";

/**
 * 终态与中间态的显示语气。
 *
 * AWAITING_APPROVAL 是 warn 而不是 danger：等人决策不是错误
 * （M0 §6 把它列为正常状态之一）。把它显示成红色会诱导操作者去"修"一个
 * 本来就正确的状态。
 */
export function statusTone(status: string): Tone {
  switch (status) {
    case "COMPLETE":
      return "ok";
    case "FAILED":
    case "BLOCKED":
      return "danger";
    case "AWAITING_APPROVAL":
      return "warn";
    default:
      return "muted";
  }
}

export function decisionTone(decision: string): Tone {
  switch (decision) {
    case "APPROVED":
      return "ok";
    case "REJECTED":
      return "danger";
    case "EXPIRED":
      return "muted";
    default:
      return "warn";
  }
}

export function stepTone(step: TraceStep): Tone {
  if (step.failureClass) {
    // 被 Policy 拒绝与执行失败都带 failureClass，但前者是系统正常工作的表现。
    // 用 warn 区分开，否则一次成功的拦截看起来像一次故障。
    return step.nodeName === "POLICY_CHECK" ? "warn" : "danger";
  }
  return "muted";
}

/** micros 转人可读的元。1 micro = 1e-6 元。 */
export function formatMicros(micros: number): string {
  if (micros === 0) {
    return "0";
  }
  const yuan = micros / 1_000_000;
  if (yuan < 0.0001) {
    return `${micros} μ`;
  }
  return `¥${yuan.toFixed(4)}`;
}

export interface BudgetRow {
  label: string;
  used: number;
  limit: number;
  /** 已用占比，limit 为 0 时返回 0 而不是 NaN。 */
  ratio: number;
  display: string;
}

/**
 * 预算展示。
 *
 * 显示「已用 / 上限」而不是只显示已用：有界执行是这个项目的三根支柱之一，
 * 一个没有上限对照的数字说不出「还剩多少」，而那正是操作者要判断的事。
 */
export function budgetRows(run: Run): BudgetRow[] {
  const rows: Array<[string, number, number, (n: number) => string]> = [
    ["步数", run.stepsUsed, run.maxSteps, String],
    ["工具调用", run.toolCallCount, run.toolCallBudget, String],
    ["token", run.tokenSpent, run.tokenBudget, (n) => n.toLocaleString()],
    ["成本", run.costSpentMicros, run.costBudgetMicros, formatMicros],
  ];
  return rows.map(([label, used, limit, fmt]) => ({
    label,
    used,
    limit,
    ratio: limit > 0 ? used / limit : 0,
    display: `${fmt(used)} / ${fmt(limit)}`,
  }));
}

/** 预算用尽时给出提示语气。0.9 以上是 danger：接近上限时 Run 随时会被终止。 */
export function budgetTone(ratio: number): Tone {
  if (ratio >= 0.9) {
    return "danger";
  }
  if (ratio >= 0.7) {
    return "warn";
  }
  return "muted";
}

export function formatInstant(value: string | null): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  // 本地时区显示，但保留秒：Trace 的相邻步骤经常在同一分钟内。
  return parsed.toLocaleString(undefined, { hour12: false });
}

/**
 * 摘要的短显示。
 *
 * 只截断不做别的：digest 的作用是让人能核对「审批时和执行时是同一份参数」，
 * 因此前缀必须是原文的前缀，不能是重新编码后的结果。
 */
export function shortDigest(digest: string): string {
  return digest.length > 16 ? `${digest.slice(0, 16)}…` : digest;
}
