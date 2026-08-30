/**
 * 控制面 HTTP 客户端。
 *
 * 三点是刻意的：
 *
 * 1. **token 存在内存里，不进 localStorage。** localStorage 里的 token 会被任何
 *    同源 XSS 拿走且长期有效。控制台是演示与运维用途，刷新后重新粘贴 token 可以接受。
 *
 * 2. **错误分类而非只抛字符串。** 401 与 403 的处置完全不同（换凭据 vs 换角色），
 *    409 是乐观锁冲突（应当重新读取后重试），把它们混成一句 "request failed"
 *    会让界面无法给出正确的下一步。
 *
 * 3. **没有写审批决策之外的写操作。** 控制台只做展示、Trace 查看与审批操作
 *    （M0 §3）。给它加上创建 Run 之类的按钮会模糊「谁是业务真相的持有者」。
 */

export type Role = "VIEWER" | "OPERATOR" | "APPROVER" | "AGENT_RUNTIME";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** 401：凭据本身不对。换 token。 */
  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  /** 403：凭据有效但角色不够。换一个有 APPROVER 的身份。 */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  /** 409：乐观锁冲突或终态已被设置。重新读取后再试。 */
  get isConflict(): boolean {
    return this.status === 409;
  }
}

function apiBase(): string {
  const injected = (globalThis as { __RUNBOOKGUARD_API_BASE__?: string })
    .__RUNBOOKGUARD_API_BASE__;
  return (injected ?? "http://127.0.0.1:8080").replace(/\/$/, "");
}

let token = "";

export function setToken(value: string): void {
  token = value.trim();
}

export function hasToken(): boolean {
  return token.length > 0;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${apiBase()}${path}`, { ...init, headers });
  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  let payload: unknown = undefined;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      // 非 JSON 响应（例如反向代理返回的 HTML 错误页）。原样带上前 200 字符，
      // 因为「返回了 HTML」本身就是最重要的线索——通常意味着请求没到控制面。
      throw new ApiError(response.status, "non_json_response", text.slice(0, 200));
    }
  }

  if (!response.ok) {
    const body = payload as { error?: string; message?: string } | undefined;
    throw new ApiError(
      response.status,
      body?.error ?? "unknown_error",
      body?.message ?? `HTTP ${response.status}`,
    );
  }
  return payload as T;
}

export interface Incident {
  incidentId: string;
  tenantId: string;
  source: string;
  severity: string;
  title: string;
  startedAt: string;
  currentStatus: string;
  version: number;
}

export interface Run {
  runId: string;
  incidentId: string;
  graphVersion: string;
  promptVersion: string;
  modelId: string;
  datasetVersion: string;
  status: string;
  currentStep: string | null;
  maxSteps: number;
  stepsUsed: number;
  deadline: string;
  costBudgetMicros: number;
  costSpentMicros: number;
  tokenBudget: number;
  tokenSpent: number;
  toolCallBudget: number;
  toolCallCount: number;
  failureClass: string | null;
  version: number;
}

export interface TraceStep {
  sequence: number;
  nodeName: string;
  status: string;
  toolCallId: string | null;
  failureClass: string | null;
  inputArtifact: string | null;
  outputArtifact: string | null;
  startedAt: string;
  finishedAt: string | null;
}

export interface TraceEvidence {
  evidenceId: string;
  sourceType: string;
  sourceIdentity: string;
  version: string;
  location: string;
  contentHash: string;
  capturedAt: string;
}

export interface Approval {
  approvalId: string;
  runId: string;
  requestedBy: string;
  toolName: string;
  resourceRef: string;
  argumentsDigest: string;
  digestAlg: string;
  argumentsCanonical: string;
  decision: string;
  decidedBy: string | null;
  expiresAt: string;
  decidedAt: string | null;
  consumedAt: string | null;
}

export interface Trace {
  run: Run;
  terminalStatus: string | null;
  steps: TraceStep[];
  evidence: TraceEvidence[];
  approvals: Approval[];
}

export interface AuditEvent {
  eventId: number;
  principalId: string;
  action: string;
  resourceType: string;
  resourceId: string | null;
  outcome: string;
  reason: string | null;
  occurredAt: string;
}

export const api = {
  listIncidents: () => request<Incident[]>("/api/v1/incidents"),
  listRuns: (incidentId: string) =>
    request<Run[]>(`/api/v1/runs?incidentId=${encodeURIComponent(incidentId)}`),
  getTrace: (runId: string) =>
    request<Trace>(`/api/v1/runs/${encodeURIComponent(runId)}/trace`),
  listPendingApprovals: () =>
    request<Approval[]>("/api/v1/approvals/pending"),
  /**
   * 审批决策。这是控制台唯一的写操作。
   *
   * 它不携带参数：审批绑定的是请求时算出的 argumentsDigest，
   * 让界面能提交一份"参数"等于给了篡改的入口。
   */
  decideApproval: (approvalId: string, approve: boolean, reason: string) =>
    request<Approval>(
      `/api/v1/approvals/${encodeURIComponent(approvalId)}/decision`,
      { method: "POST", body: JSON.stringify({ approve, reason }) },
    ),
  listAuditEvents: (resourceType?: string, resourceId?: string) => {
    const params = new URLSearchParams({ limit: "50" });
    if (resourceType && resourceId) {
      params.set("resourceType", resourceType);
      params.set("resourceId", resourceId);
    }
    return request<AuditEvent[]>(`/api/v1/audit-events?${params.toString()}`);
  },
};
