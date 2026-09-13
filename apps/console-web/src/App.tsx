import { useCallback, useEffect, useState } from "react";
import { ApiError, api, hasToken, setToken } from "./api/client";
import { IncidentsPage } from "./pages/IncidentsPage";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { AuditPage } from "./pages/AuditPage";

type Tab = "incidents" | "approvals" | "audit";

/**
 * 顶层壳。它只做三件事：持有 token、切页、把 API 错误翻译成人能行动的提示。
 *
 * 刻意没有路由库：三个页面、没有深链需求，加 react-router 只是多一层依赖。
 */
export function App() {
  const [tab, setTab] = useState<Tab>("incidents");
  const [tokenInput, setTokenInput] = useState("");
  const [authed, setAuthed] = useState(false);
  const [error, setError] = useState<ApiError | Error | null>(null);

  const applyToken = useCallback(() => {
    setToken(tokenInput);
    setAuthed(hasToken());
    setError(null);
  }, [tokenInput]);

  useEffect(() => {
    // 本地演示用的固定 token（DevDataSeeder 写入的）。只在明显是本地地址时预填，
    // 免得部署到别处的控制台把一个演示 token 摆在输入框里。
    const base = (globalThis as { __RUNBOOKGUARD_API_BASE__?: string })
      .__RUNBOOKGUARD_API_BASE__;
    if (base && /127\.0\.0\.1|localhost/.test(base)) {
      setTokenInput("dev-approver-token");
    }
  }, []);

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <div className="brand-mark">RG</div>
          <h1>RunbookGuard 控制台</h1>
        </div>
        <span className="subtitle">
          诊断展示 · Trace 查看 · 审批操作 —— 业务真相在 Java 控制面
        </span>
      </header>

      <div className="panel token-bar">
        <label htmlFor="token">访问凭据</label>
        <input
          id="token"
          type="password"
          value={tokenInput}
          onChange={(e) => setTokenInput(e.target.value)}
          placeholder="dev-approver-token"
          autoComplete="off"
        />
        <button className="primary" onClick={applyToken}>
          使用
        </button>
        <span className="note">
          仅存于内存，刷新后需重新输入
        </span>
      </div>

      {error ? <ErrorBanner error={error} /> : null}

      <nav className="tabs" role="tablist">
        <button
          role="tab"
          aria-selected={tab === "incidents"}
          onClick={() => setTab("incidents")}
        >
          Incident 与 Run
        </button>
        <button
          role="tab"
          aria-selected={tab === "approvals"}
          onClick={() => setTab("approvals")}
        >
          待审批
        </button>
        <button
          role="tab"
          aria-selected={tab === "audit"}
          onClick={() => setTab("audit")}
        >
          审计事件
        </button>
      </nav>

      {!authed ? (
        <div className="panel">
          <p className="hint">
            先填入 token。本地 compose 环境的演示凭据见 README：
            <code>dev-viewer-token</code>（只读）、
            <code>dev-approver-token</code>（可审批）。
          </p>
        </div>
      ) : tab === "incidents" ? (
        <IncidentsPage onError={setError} />
      ) : tab === "approvals" ? (
        <ApprovalsPage onError={setError} />
      ) : (
        <AuditPage onError={setError} />
      )}
    </div>
  );
}

/**
 * 错误提示要说出**下一步做什么**。
 *
 * "request failed" 对操作者没有价值：401 要换 token，403 要换角色，
 * 409 要重新读取。区分不出来时界面会诱导人去做错的事。
 */
function ErrorBanner({ error }: { error: ApiError | Error }) {
  const advice = adviceFor(error);
  return (
    <div className="error-banner">
      <div>
        <span className="code">
          {error instanceof ApiError ? `${error.status} ${error.code}` : error.name}
        </span>
        {error.message}
      </div>
      {advice ? <div className="muted">{advice}</div> : null}
    </div>
  );
}

function adviceFor(error: ApiError | Error): string | null {
  if (!(error instanceof ApiError)) {
    // 网络层失败最常见的原因是控制面没起来或 CORS 未放行当前来源。
    return "请求没有到达控制面：检查控制面是否在运行，以及 CONSOLE_ALLOWED_ORIGINS 是否包含当前地址。";
  }
  if (error.isUnauthenticated) {
    return "凭据无效。换一个 token。";
  }
  if (error.isForbidden) {
    return "凭据有效但角色不足。审批需要 APPROVER；AGENT_RUNTIME 刻意不含该角色。";
  }
  if (error.isConflict) {
    return "状态已被别人改过（乐观锁冲突或终态已设置）。刷新后再试。";
  }
  if (error.status === 429) {
    return "触发限流。等一分钟。";
  }
  if (error.status === 404) {
    return "对象不存在，或不属于当前租户 —— 跨租户访问一律返回 404，不泄漏存在性。";
  }
  return null;
}

/** 页面共用的加载封装。把 ApiError 原样交给上层，不在这里吞掉。 */
export function useAsync<T>(
  load: () => Promise<T>,
  onError: (e: ApiError | Error) => void,
  deps: unknown[],
): { data: T | null; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    load()
      .then((value) => {
        if (!cancelled) {
          setData(value);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          onError(e instanceof Error ? e : new Error(String(e)));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return { data, loading, reload: () => setNonce((n) => n + 1) };
}

export { api };
