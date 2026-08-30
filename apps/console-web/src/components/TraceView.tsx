import type { ApiError, Trace } from "../api/client";
import { api } from "../api/client";
import { useAsync } from "../App";
import {
  budgetRows,
  budgetTone,
  decisionTone,
  formatInstant,
  shortDigest,
  statusTone,
  stepTone,
} from "./format";

interface Props {
  runId: string;
  onError: (e: ApiError | Error) => void;
}

/**
 * Trace 视图：时间线、预算、证据与引用、审批链路。
 *
 * 四块都必须在同一屏能看到，因为一次审查要回答的问题是同一个：
 * 「它凭什么下的结论、有没有越界」。分成四个页面就需要人自己拼起来。
 */
export function TraceView({ runId, onError }: Props) {
  const trace = useAsync<Trace>(() => api.getTrace(runId), onError, [runId]);

  if (trace.loading && !trace.data) {
    return (
      <section className="panel">
        <h2>Trace</h2>
        <p className="hint">加载中…</p>
      </section>
    );
  }
  if (!trace.data) {
    return null;
  }

  const { run, steps, evidence, approvals, terminalStatus } = trace.data;
  const citations = evidence.filter((e) => e.sourceType === "retrieve_runbook_section");

  return (
    <>
      <section className="panel">
        <h2>
          Run {run.runId}{" "}
          <span className={`badge ${statusTone(run.status)}`}>{run.status}</span>
          {terminalStatus ? (
            <span className={`badge ${statusTone(terminalStatus)}`}>
              终态 {terminalStatus}
            </span>
          ) : null}
        </h2>
        <p className="hint">
          终态由 run_terminal_state 的主键裁决，唯一。重复投递不会产生第二个终态。
        </p>
        <div className="budget-grid">
          {budgetRows(run).map((row) => (
            <div className="budget-cell" key={row.label}>
              <div className="label">{row.label}</div>
              <div className={`value ${budgetTone(row.ratio)}`}>{row.display}</div>
            </div>
          ))}
          <div className="budget-cell">
            <div className="label">deadline</div>
            <div className="value">{formatInstant(run.deadline)}</div>
          </div>
        </div>
      </section>

      <div className="columns">
        <section className="panel">
          <h2>时间线（{steps.length} 步）</h2>
          <p className="hint">
            带 failureClass 的步骤里，POLICY_CHECK 上的是**成功的拦截**，
            不是故障 —— 用不同颜色区分开。
          </p>
          <ul className="timeline">
            {steps.map((step) => (
              <li
                key={step.sequence}
                className={
                  step.failureClass
                    ? step.nodeName === "POLICY_CHECK"
                      ? "denied"
                      : "failed"
                    : ""
                }
              >
                <div>
                  <span className="node">{step.nodeName}</span>{" "}
                  <span className="muted">#{step.sequence}</span>{" "}
                  {step.failureClass ? (
                    <span className={`badge ${stepTone(step)}`}>
                      {step.failureClass}
                    </span>
                  ) : null}
                </div>
                {step.toolCallId ? (
                  <div className="mono muted">tool: {step.toolCallId}</div>
                ) : null}
                {step.outputArtifact ? (
                  <pre>{step.outputArtifact}</pre>
                ) : null}
                <div className="mono muted">
                  {formatInstant(step.startedAt)}
                  {step.finishedAt ? ` → ${formatInstant(step.finishedAt)}` : ""}
                </div>
              </li>
            ))}
            {steps.length === 0 ? (
              <li className="muted">
                这个 Run 还没有上报步骤。Agent Runtime 通过
                POST /api/v1/runs/{"{runId}"}/trace 上报。
              </li>
            ) : null}
          </ul>
        </section>

        <div>
          <section className="panel">
            <h2>证据（{evidence.length}）</h2>
            <p className="hint">
              contentHash 是引用能被反查的前提：没有它，「有引用」只是一个说法。
            </p>
            <table>
              <thead>
                <tr>
                  <th>来源</th>
                  <th>定位</th>
                  <th>content_hash</th>
                </tr>
              </thead>
              <tbody>
                {evidence.map((item) => (
                  <tr key={item.evidenceId}>
                    <td>
                      {item.sourceType}
                      <div className="mono muted">{item.evidenceId}</div>
                    </td>
                    <td className="mono">
                      {item.sourceIdentity}
                      <div className="muted">
                        {item.location} @ {item.version}
                      </div>
                    </td>
                    <td className="mono">{shortDigest(item.contentHash)}</td>
                  </tr>
                ))}
                {evidence.length === 0 ? (
                  <tr>
                    <td colSpan={3} className="muted">
                      没有证据。若结论是 insufficient_evidence，这是正确的状态。
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
            {citations.length > 0 ? (
              <p className="hint">
                其中 {citations.length} 条是 Runbook 引用，可按
                document_version + section_id + content_hash 反查原文。
              </p>
            ) : null}
          </section>

          <section className="panel">
            <h2>审批链路（{approvals.length}）</h2>
            <p className="hint">
              审批绑定的是 argumentsDigest。执行前会重新比对四项
              （审批 id / 工具名 / 资源 / 参数摘要），任一不符即拒绝执行。
            </p>
            <table>
              <thead>
                <tr>
                  <th>工具与资源</th>
                  <th>决策</th>
                  <th>摘要</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                {approvals.map((approval) => (
                  <tr key={approval.approvalId}>
                    <td>
                      {approval.toolName}
                      <div className="mono muted">{approval.resourceRef}</div>
                    </td>
                    <td>
                      <span className={`badge ${decisionTone(approval.decision)}`}>
                        {approval.decision}
                      </span>
                      {approval.consumedAt ? (
                        <div className="mono muted">已消费</div>
                      ) : null}
                    </td>
                    <td className="mono" title={approval.argumentsDigest}>
                      {shortDigest(approval.argumentsDigest)}
                      <div className="muted">{approval.digestAlg}</div>
                    </td>
                    <td className="mono muted">
                      到期 {formatInstant(approval.expiresAt)}
                      {approval.decidedAt ? (
                        <div>决策 {formatInstant(approval.decidedAt)}</div>
                      ) : null}
                    </td>
                  </tr>
                ))}
                {approvals.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="muted">
                      这个 Run 没有请求过写动作。
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </section>
        </div>
      </div>
    </>
  );
}
