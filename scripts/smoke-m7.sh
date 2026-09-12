#!/usr/bin/env bash
# M7 冒烟：一台干净机器上 compose 起来之后，演示流程能不能走通。
#
# 它检验的是**接线**，不是业务正确性（那是 M6 的评测干的事）：
# 端点是否可达、CORS 是否放行控制台、指标是否真的被 Prometheus 抓到、
# Trace 是否能写进去再读回来。
#
# 前置：docker compose -f deploy/compose/docker-compose.yml up -d
#       bash scripts/wait-for-stack.sh
set -u

CONTROL_PLANE="${CONTROL_PLANE:-http://127.0.0.1:8080}"
AGENT_RUNTIME="${AGENT_RUNTIME:-http://127.0.0.1:8100}"
SYNTHETIC_LAB="${SYNTHETIC_LAB:-http://127.0.0.1:8090}"
CONSOLE="${CONSOLE:-http://127.0.0.1:8081}"
PROMETHEUS="${PROMETHEUS:-http://127.0.0.1:9090}"
GRAFANA="${GRAFANA:-http://127.0.0.1:3000}"
CONSOLE_ORIGIN="${CONSOLE_ORIGIN:-http://127.0.0.1:8081}"
MANAGEMENT="${MANAGEMENT:-http://127.0.0.1:9080}"

OPERATOR_TOKEN="${OPERATOR_TOKEN:-dev-operator-token}"
APPROVER_TOKEN="${APPROVER_TOKEN:-dev-approver-token}"
AGENT_TOKEN="${AGENT_TOKEN:-dev-agent-token}"
VIEWER_TOKEN="${VIEWER_TOKEN:-dev-viewer-token}"

PASS=0
FAIL=0

check() {
  local name="$1" ok="$2" detail="${3:-}"
  if [ "$ok" = "1" ]; then
    printf 'ok    %s\n' "$name"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %s\n' "$name"
    [ -n "$detail" ] && printf '      %s\n' "$detail"
    FAIL=$((FAIL + 1))
  fi
}

json_field() {
  python3 -c "
import json,sys
data = json.load(sys.stdin)
for key in '$1'.split('.'):
    if isinstance(data, list):
        data = data[int(key)]
    else:
        data = data.get(key)
    if data is None:
        break
print(data if data is not None else '')
"
}

echo "== 1. 服务可达 =="
check "control-plane readiness（管理端口）" \
  "$([ "$(curl -s -o /dev/null -w '%{http_code}' "${MANAGEMENT}/actuator/health/readiness")" = 200 ] && echo 1 || echo 0)"
# 业务端口上 actuator 必须不复存在——这是端口分离的意义所在。
check "业务端口不再暴露 actuator" \
  "$([ "$(curl -s -o /dev/null -w '%{http_code}' "${CONTROL_PLANE}/actuator/health")" = 404 ] && echo 1 || echo 0)"
check "agent-runtime health" \
  "$([ "$(curl -s -o /dev/null -w '%{http_code}' "${AGENT_RUNTIME}/health")" = 200 ] && echo 1 || echo 0)"
check "synthetic-lab health" \
  "$([ "$(curl -s -o /dev/null -w '%{http_code}' "${SYNTHETIC_LAB}/health")" = 200 ] && echo 1 || echo 0)"
check "console index" \
  "$([ "$(curl -s -o /dev/null -w '%{http_code}' "${CONSOLE}/index.html")" = 200 ] && echo 1 || echo 0)"

echo
echo "== 2. 控制台的 API 地址被注入（不是构建期烧死的占位值） =="
INJECTED="$(curl -s "${CONSOLE}/index.html" | grep -o '__RUNBOOKGUARD_API_BASE__ = "[^"]*"' | head -1)"
check "index.html 含 API base" \
  "$([ -n "$INJECTED" ] && echo 1 || echo 0)" "$INJECTED"

echo
echo "== 3. CORS 放行控制台，拒绝陌生来源 =="
ALLOW_HEADER="$(curl -s -D - -o /dev/null -X OPTIONS \
  -H "Origin: ${CONSOLE_ORIGIN}" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: authorization" \
  "${CONTROL_PLANE}/api/v1/incidents" | grep -i '^access-control-allow-origin' | tr -d '\r')"
check "预检放行控制台来源" \
  "$(printf '%s' "$ALLOW_HEADER" | grep -qi "$CONSOLE_ORIGIN" && echo 1 || echo 0)" "$ALLOW_HEADER"

EVIL_STATUS="$(curl -s -o /dev/null -w '%{http_code}' -X OPTIONS \
  -H "Origin: http://evil.example" \
  -H "Access-Control-Request-Method: GET" \
  "${CONTROL_PLANE}/api/v1/incidents")"
# 未放行的来源必须**不**拿到 200 预检通过。通配 * 会让这一条失败。
check "预检拒绝陌生来源" \
  "$([ "$EVIL_STATUS" != "200" ] && echo 1 || echo 0)" "HTTP $EVIL_STATUS"

echo
echo "== 4. 认证与授权 =="
check "无凭据 -> 401" \
  "$([ "$(curl -s -o /dev/null -w '%{http_code}' "${CONTROL_PLANE}/api/v1/incidents")" = 401 ] && echo 1 || echo 0)"
check "VIEWER 读 Incident -> 200" \
  "$([ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${VIEWER_TOKEN}" "${CONTROL_PLANE}/api/v1/incidents")" = 200 ] && echo 1 || echo 0)"
check "VIEWER 建 Incident -> 403" \
  "$([ "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer ${VIEWER_TOKEN}" -H 'Content-Type: application/json' -d '{"source":"synthetic-lab","severity":"P2","title":"x"}' "${CONTROL_PLANE}/api/v1/incidents")" = 403 ] && echo 1 || echo 0)"

echo
echo "== 5. 演示流程：Incident -> Run -> Trace -> 审批 =="
INCIDENT_ID="$(curl -s -X POST -H "Authorization: Bearer ${OPERATOR_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"source":"synthetic-lab","severity":"P1","title":"smoke-m7 连接池耗尽"}' \
  "${CONTROL_PLANE}/api/v1/incidents" | json_field incidentId)"
check "创建 Incident" "$([ -n "$INCIDENT_ID" ] && echo 1 || echo 0)" "$INCIDENT_ID"

RUN_ID="$(curl -s -X POST -H "Authorization: Bearer ${AGENT_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"incidentId\":\"${INCIDENT_ID}\",\"graphVersion\":\"langgraph-v1\",\"promptVersion\":\"p1\",\"modelId\":\"fake-model\",\"datasetVersion\":\"incidents-dev\"}" \
  "${CONTROL_PLANE}/api/v1/runs" | json_field runId)"
check "创建 Run" "$([ -n "$RUN_ID" ] && echo 1 || echo 0)" "$RUN_ID"

TRACE_POST="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer ${AGENT_TOKEN}" -H 'Content-Type: application/json' \
  -d '{"steps":[{"sequence":1,"nodeName":"COLLECT_CONTEXT","status":"COMPLETED"},{"sequence":2,"nodeName":"POLICY_CHECK","status":"DENIED","failureClass":"authorization_failed"}],"evidence":[{"evidenceId":"ev-smoke0000001","sourceType":"get_service_metrics","sourceIdentity":"service:synthetic-orders","version":"v1","location":"lab:/v1/metrics","contentHash":"'"$(printf 'a%.0s' $(seq 1 64))"'"}]}' \
  "${CONTROL_PLANE}/api/v1/runs/${RUN_ID}/trace")"
check "上报 Trace -> 200" "$([ "$TRACE_POST" = 200 ] && echo 1 || echo 0)" "HTTP $TRACE_POST"

TRACE_BODY="$(curl -s -H "Authorization: Bearer ${VIEWER_TOKEN}" \
  "${CONTROL_PLANE}/api/v1/runs/${RUN_ID}/trace")"
STEP_COUNT="$(printf '%s' "$TRACE_BODY" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['steps']))")"
check "VIEWER 读回 2 步" "$([ "$STEP_COUNT" = 2 ] && echo 1 || echo 0)" "steps=$STEP_COUNT"

APPROVAL_ID="$(curl -s -X POST -H "Authorization: Bearer ${AGENT_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"runId\":\"${RUN_ID}\",\"toolName\":\"rollback_synthetic_deployment\",\"resourceRef\":\"svc:synthetic-orders\",\"arguments\":{\"service\":\"synthetic-orders\",\"target_version\":\"v1.4.2\"}}" \
  "${CONTROL_PLANE}/api/v1/approvals" | json_field approvalId)"
check "发起审批" "$([ -n "$APPROVAL_ID" ] && echo 1 || echo 0)" "$APPROVAL_ID"

# AGENT_RUNTIME 不含 APPROVER：Python 侧不存在能放行的代码路径（M0 INV-4）。
SELF_APPROVE="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer ${AGENT_TOKEN}" -H 'Content-Type: application/json' \
  -d '{"approve":true,"reason":"self"}' \
  "${CONTROL_PLANE}/api/v1/approvals/${APPROVAL_ID}/decision")"
check "AGENT_RUNTIME 自批 -> 403" \
  "$([ "$SELF_APPROVE" = 403 ] && echo 1 || echo 0)" "HTTP $SELF_APPROVE"

DECISION="$(curl -s -X POST -H "Authorization: Bearer ${APPROVER_TOKEN}" \
  -H 'Content-Type: application/json' -d '{"approve":true,"reason":"smoke"}' \
  "${CONTROL_PLANE}/api/v1/approvals/${APPROVAL_ID}/decision" | json_field decision)"
check "APPROVER 批准 -> APPROVED" \
  "$([ "$DECISION" = "APPROVED" ] && echo 1 || echo 0)" "$DECISION"

# 审批后篡改参数：摘要不符必须被拒（威胁 T-2）。
TAMPERED="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer ${AGENT_TOKEN}" -H 'Content-Type: application/json' \
  -d '{"toolName":"rollback_synthetic_deployment","resourceRef":"svc:synthetic-orders","arguments":{"service":"synthetic-orders","target_version":"v0.0.1"}}' \
  "${CONTROL_PLANE}/api/v1/approvals/${APPROVAL_ID}/consume")"
check "篡改参数后 consume -> 403" \
  "$([ "$TAMPERED" = 403 ] && echo 1 || echo 0)" "HTTP $TAMPERED"

echo
echo "== 6. 诊断链路（fake provider，零真实调用） =="
DIAGNOSE="$(curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"system","content":"you are a diagnostic assistant"},{"role":"user","content":"Incident: synthetic-orders p99 3s\n\nCollected evidence:\n- ev-abc123456789 source=get_service_metrics identity=service:synthetic-orders hash=aaaaaaaaaaaa untrusted=True"}]}' \
  "${AGENT_RUNTIME}/v1/diagnose")"
CONCLUSION="$(printf '%s' "$DIAGNOSE" | json_field diagnosis.conclusion_type)"
check "结构化诊断返回 conclusion_type" \
  "$([ -n "$CONCLUSION" ] && echo 1 || echo 0)" "$CONCLUSION"

echo
echo "== 7. 观测接线 =="
check "agent-runtime /metrics" \
  "$(curl -s "${AGENT_RUNTIME}/metrics" | grep -q 'runbookguard_http_requests_total' && echo 1 || echo 0)"
check "control-plane /actuator/prometheus（管理端口）" \
  "$(curl -s "${MANAGEMENT}/actuator/prometheus" | grep -q 'http_server_requests_seconds' && echo 1 || echo 0)"
check "synthetic-lab /metrics" \
  "$(curl -s "${SYNTHETIC_LAB}/metrics" | grep -q 'synthetic_lab_requests_total' && echo 1 || echo 0)"

# 目标 up 而不只是「端点能打开」：Prometheus 抓不到目标时面板全空，
# 而那与「没有流量」的表现完全一样。
UP_TARGETS="$(curl -s "${PROMETHEUS}/api/v1/query?query=up" | python3 -c "
import json,sys
data = json.load(sys.stdin)
rows = data.get('data', {}).get('result', [])
up = [r['metric'].get('job') for r in rows if r['value'][1] == '1']
print(','.join(sorted(j for j in up if j)))
" 2>/dev/null)"
for job in control-plane agent-runtime synthetic-lab; do
  check "Prometheus 抓到 ${job}" \
    "$(printf '%s' "$UP_TARGETS" | grep -q "$job" && echo 1 || echo 0)" "up=$UP_TARGETS"
done

check "Grafana 数据源已 provision" \
  "$(curl -s "${GRAFANA}/api/health" | grep -q '"database": *"ok"' && echo 1 || echo 0)"
check "Grafana 面板已 provision" \
  "$(curl -s "${GRAFANA}/api/search?query=RunbookGuard" | grep -q 'runbookguard-overview' && echo 1 || echo 0)"

echo
echo "== 8. Trace 导出 =="
TRACING="$(curl -s "${AGENT_RUNTIME}/health" | json_field tracing_enabled)"
check "agent-runtime trace 已启用" \
  "$([ "$TRACING" = "True" ] || [ "$TRACING" = "true" ] && echo 1 || echo 0)" "tracing_enabled=$TRACING"

# span 计数**增长**，不只是端点可达。
#
# 「配置了导出器」与「span 真的到了 collector」是两件事：前者只要写对 yaml，
# 后者要求网络可达、协议对得上、采样没把它全丢掉。只查配置会让一条断掉的
# trace 链路看起来是好的。
collector_spans() {
  curl -s "http://127.0.0.1:${OTEL_METRICS_PORT:-8889}/metrics" 2>/dev/null \
    | grep '^otelcol_receiver_accepted_spans{' | awk '{print $2}' | head -1
}
SPANS_BEFORE="$(collector_spans)"
SPANS_BEFORE="${SPANS_BEFORE:-0}"
# 两侧各打一次：Java 走 micrometer-tracing-bridge-otel，Python 走 otel-sdk，
# 只测一侧无法发现另一侧的链路断了。
curl -s -o /dev/null -H "Authorization: Bearer ${VIEWER_TOKEN}" "${CONTROL_PLANE}/api/v1/incidents"
curl -s -o /dev/null -X POST -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Incident: smoke\n\nCollected evidence:\n- ev-smoke0000001 source=get_service_metrics identity=service:synthetic-orders hash=aaa untrusted=True"}]}' \
  "${AGENT_RUNTIME}/v1/diagnose"
# BatchSpanProcessor 的默认导出间隔是 5s，等两个周期。
sleep 12
SPANS_AFTER="$(collector_spans)"
SPANS_AFTER="${SPANS_AFTER:-0}"
check "collector 收到新 span" \
  "$(python3 -c "print(1 if float('${SPANS_AFTER}') > float('${SPANS_BEFORE}') else 0)")" \
  "accepted_spans ${SPANS_BEFORE} -> ${SPANS_AFTER}"

echo
echo "========================================"
echo "通过 ${PASS}  失败 ${FAIL}"
if [ "$FAIL" -gt 0 ]; then
  echo "M7 冒烟未通过。"
  exit 1
fi
echo "M7 冒烟通过。"
