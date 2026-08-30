import type { ApiError, AuditEvent } from "../api/client";
import { api } from "../api/client";
import { useAsync } from "../App";
import { formatInstant } from "../components/format";

interface Props {
  onError: (e: ApiError | Error) => void;
}

/**
 * 审计事件。只读，因为它在数据库层就是只增的——V2 migration 的触发器会拒绝
 * UPDATE 与 DELETE，Mapper 里也没有对应方法。
 *
 * 被拒绝的事件（outcome=DENIED）比允许的更重要：它们记录了有人尝试过什么。
 * 因此展示上不做过滤，全部按时间倒序列出。
 */
export function AuditPage({ onError }: Props) {
  const events = useAsync<AuditEvent[]>(() => api.listAuditEvents(), onError, []);
  const items = events.data ?? [];

  return (
    <section className="panel">
      <h2>审计事件（最近 {items.length} 条）</h2>
      <p className="hint">
        追加式存储：数据库触发器拒绝 UPDATE 与 DELETE。拒绝类事件用 REQUIRES_NEW
        事务写入，因此「越权尝试被拒绝并回滚」时审计记录仍然留存。
      </p>
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>动作</th>
            <th>结果</th>
            <th>主体</th>
            <th>对象</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>
          {items.map((event) => (
            <tr key={event.eventId}>
              <td className="mono muted">{formatInstant(event.occurredAt)}</td>
              <td className="mono">{event.action}</td>
              <td>
                <span
                  className={`badge ${event.outcome === "DENIED" ? "danger" : "ok"}`}
                >
                  {event.outcome}
                </span>
              </td>
              <td className="mono muted">{event.principalId}</td>
              <td className="mono muted">
                {event.resourceType}
                {event.resourceId ? `:${event.resourceId}` : ""}
              </td>
              <td className="muted">{event.reason ?? "—"}</td>
            </tr>
          ))}
          {items.length === 0 && !events.loading ? (
            <tr>
              <td colSpan={6} className="muted">
                当前租户还没有审计事件。
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </section>
  );
}
