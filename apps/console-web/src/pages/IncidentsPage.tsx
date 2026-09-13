import { useState } from "react";
import type { ApiError, Incident, Run } from "../api/client";
import { api } from "../api/client";
import { useAsync } from "../App";
import { statusTone } from "../components/format";
import { TraceView } from "../components/TraceView";

interface Props {
  onError: (e: ApiError | Error) => void;
}

/**
 * Incident → Run → Trace 三级下钻。
 *
 * 选中态放在 URL 之外（组件 state）：控制台是运维现场工具，不需要分享深链，
 * 加上路由同步只会引入"URL 与界面不一致"这一类新 bug。
 */
export function IncidentsPage({ onError }: Props) {
  const [selectedIncident, setSelectedIncident] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);

  const incidents = useAsync<Incident[]>(() => api.listIncidents(), onError, []);
  const runs = useAsync<Run[]>(
    () => (selectedIncident ? api.listRuns(selectedIncident) : Promise.resolve([])),
    onError,
    [selectedIncident],
  );

  return (
    <>
      {/* Trace 视图放在列表**上方**：点击 Run 后它立即可见。
          此前它在页面最底部，要滚过整份 20 行的 Incident 列表才能看到——
          用户实际使用时完全找不到它。 */}
      {selectedRun ? <TraceView runId={selectedRun} onError={onError} /> : null}

      <div className="columns">
        <section className="panel">
          <h2>Incident</h2>
          <p className="hint">
            {incidents.loading ? "加载中…" : `${incidents.data?.length ?? 0} 条`}
            {selectedRun ? "（已选中一条 Run，其 Trace 在页面顶部）" : ""}
          </p>
          <table>
            <thead>
              <tr>
                <th>标题</th>
                <th>级别</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {(incidents.data ?? []).map((incident) => (
                <tr
                  key={incident.incidentId}
                  className={
                    "selectable" +
                    (incident.incidentId === selectedIncident ? " selected" : "")
                  }
                  onClick={() => {
                    setSelectedIncident(incident.incidentId);
                    setSelectedRun(null);
                  }}
                >
                  <td>
                    {incident.title}
                    <div className="mono muted">{incident.incidentId}</div>
                  </td>
                  <td>{incident.severity}</td>
                  <td>
                    <span className="badge muted">{incident.currentStatus}</span>
                  </td>
                </tr>
              ))}
              {!incidents.loading && (incidents.data ?? []).length === 0 ? (
                <tr>
                  <td colSpan={3} className="muted">
                    还没有 Incident。用 scripts/smoke-m1.sh 造一个，
                    或从 synthetic-lab 起一个剧本后由 Agent Runtime 创建。
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </section>

        <section className="panel">
          <h2>Run</h2>
          <p className="hint">
            {selectedIncident
              ? "每个 Run 都记录了它用哪套配置跑的（graph / prompt / model / dataset 版本）与预算上限。"
              : "先在左边选一个 Incident。"}
          </p>
          {selectedIncident ? (
            <table>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>状态</th>
                  <th>配置</th>
                  <th>步数</th>
                </tr>
              </thead>
              <tbody>
                {(runs.data ?? []).map((run) => (
                  <tr
                    key={run.runId}
                    className={
                      "selectable" + (run.runId === selectedRun ? " selected" : "")
                    }
                    onClick={() => setSelectedRun(run.runId)}
                  >
                    <td className="mono">{run.runId}</td>
                    <td>
                      <span className={`badge ${statusTone(run.status)}`}>
                        {run.status}
                      </span>
                      {run.failureClass ? (
                        <div className="mono muted">{run.failureClass}</div>
                      ) : null}
                    </td>
                    <td className="mono muted">
                      {run.graphVersion} / {run.promptVersion}
                      <br />
                      {run.modelId} / {run.datasetVersion}
                    </td>
                    <td className="mono">
                      {run.stepsUsed}/{run.maxSteps}
                    </td>
                  </tr>
                ))}
                {!runs.loading && (runs.data ?? []).length === 0 ? (
                  <tr>
                    <td colSpan={4} className="muted">
                      这个 Incident 还没有 Run。
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          ) : null}
        </section>
      </div>

    </>
  );
}
