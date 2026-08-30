import { useState } from "react";
import type { ApiError, Approval } from "../api/client";
import { api } from "../api/client";
import { useAsync } from "../App";
import { formatInstant, shortDigest } from "../components/format";

interface Props {
  onError: (e: ApiError | Error) => void;
}

/**
 * 待审批列表与决策。这是控制台唯一的写操作。
 *
 * 界面**不提供**编辑参数的入口：审批绑定的是请求时算出的 argumentsDigest，
 * 让人在这里改参数等于把「审批后篡改参数」这条攻击路径开在自己的 UI 上。
 * 参数只读展示，供审批人核对。
 */
export function ApprovalsPage({ onError }: Props) {
  const pending = useAsync<Approval[]>(() => api.listPendingApprovals(), onError, []);
  const [busy, setBusy] = useState<string | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [decided, setDecided] = useState<Record<string, string>>({});

  async function decide(approval: Approval, approve: boolean) {
    const reason = (reasons[approval.approvalId] ?? "").trim();
    if (!approve && !reason) {
      // 驳回必须给理由：一个没有理由的驳回让请求方无法判断该改什么再来。
      onError(new Error("驳回需要填写理由。"));
      return;
    }
    setBusy(approval.approvalId);
    try {
      const result = await api.decideApproval(approval.approvalId, approve, reason);
      setDecided((prev) => ({ ...prev, [approval.approvalId]: result.decision }));
      pending.reload();
    } catch (e: unknown) {
      onError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setBusy(null);
    }
  }

  const items = pending.data ?? [];

  return (
    <section className="panel">
      <h2>待审批（{items.length}）</h2>
      <p className="hint">
        决策需要 APPROVER 角色。发起人不能批准自己的请求（四眼原则），
        AGENT_RUNTIME 角色刻意不含 APPROVER —— Python 侧不存在能放行的代码路径。
      </p>

      {items.length === 0 && !pending.loading ? (
        <p className="muted">
          没有待审批请求。跑一个会提议写动作的 Run 会产生一条
          （例如 scripts/drill-m4.py）。
        </p>
      ) : null}

      {items.map((approval) => {
        const done = decided[approval.approvalId];
        return (
          <div className="panel" key={approval.approvalId}>
            <div>
              <strong>{approval.toolName}</strong>{" "}
              <span className="mono muted">{approval.resourceRef}</span>
            </div>
            <div className="mono muted">
              run {approval.runId} · 发起 {approval.requestedBy} · 到期{" "}
              {formatInstant(approval.expiresAt)}
            </div>

            <div className="hint">
              参数原文（只读，供核对）。摘要{" "}
              <span className="mono" title={approval.argumentsDigest}>
                {shortDigest(approval.argumentsDigest)}
              </span>{" "}
              <span className="muted">{approval.digestAlg}</span>
            </div>
            <pre>{approval.argumentsCanonical}</pre>

            {done ? (
              <div className="muted">已决策：{done}</div>
            ) : (
              <div className="token-bar">
                <input
                  placeholder="理由（驳回时必填）"
                  value={reasons[approval.approvalId] ?? ""}
                  onChange={(e) =>
                    setReasons((prev) => ({
                      ...prev,
                      [approval.approvalId]: e.target.value,
                    }))
                  }
                />
                <button
                  className="primary"
                  disabled={busy === approval.approvalId}
                  onClick={() => decide(approval, true)}
                >
                  批准
                </button>
                <button
                  className="danger"
                  disabled={busy === approval.approvalId}
                  onClick={() => decide(approval, false)}
                >
                  驳回
                </button>
              </div>
            )}
          </div>
        );
      })}
    </section>
  );
}
