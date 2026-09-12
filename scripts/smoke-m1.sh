#!/usr/bin/env bash
# M1 端到端冒烟：对运行中的 Control Plane 打真实 HTTP 请求。
# 与 MockMvc 测试互补——这里验证的是"打包成镜像、连真实 MySQL/Redis 起来之后仍然对"。
#
# 前置：deploy/compose 已 up 且 control-plane 健康，且以 RUNBOOKGUARD_SEED_DEV_DATA=true 启动。
set -u

BASE="${BASE:-http://127.0.0.1:8080}"
OP="Authorization: Bearer dev-operator-token"
AG="Authorization: Bearer dev-agent-token"
AP="Authorization: Bearer dev-approver-token"
VW="Authorization: Bearer dev-viewer-token"
JSON="Content-Type: application/json"

PASS=0
FAIL=0

# check <label> <expected-status> <curl-args...>
check() {
  local label="$1" expected="$2"; shift 2
  local out status body
  out="$(curl -s -w '\n%{http_code}' "$@")"
  status="$(printf '%s' "$out" | tail -1)"
  body="$(printf '%s' "$out" | sed '$d')"
  if [ "$status" = "$expected" ]; then
    printf 'ok    %-52s [%s]\n' "$label" "$status"
    PASS=$((PASS + 1))
    LAST_BODY="$body"
  else
    printf 'FAIL  %-52s expected %s got %s\n      %s\n' "$label" "$expected" "$status" "$body"
    FAIL=$((FAIL + 1))
    LAST_BODY="$body"
  fi
}

jsonfield() {
  printf '%s' "$1" | python3 -c "import sys,json;print(json.load(sys.stdin)[sys.argv[1]])" "$2"
}

echo "== 健康与认证 =="
# actuator 在独立管理端口（M7 §7.5 整改）；业务口只服务 /api/**。
MGMT="${MGMT:-http://127.0.0.1:9080}"
check "健康检查无需认证（管理端口）" 200 "$MGMT/actuator/health"
check "无凭据访问 -> 401" 401 "$BASE/api/v1/incidents"
check "伪造 token -> 401" 401 -H "Authorization: Bearer forged" "$BASE/api/v1/incidents"

echo
echo "== RBAC =="
check "VIEWER 创建 Incident -> 403" 403 -X POST -H "$VW" -H "$JSON" \
  -d '{"source":"synthetic-lab","severity":"P2","title":"viewer attempt"}' \
  "$BASE/api/v1/incidents"

echo
echo "== 成功路径：Incident -> Run -> Approval -> Consume =="
check "OPERATOR 创建 Incident -> 201" 201 -X POST -H "$OP" -H "$JSON" \
  -d '{"source":"synthetic-lab","severity":"P1","title":"connection pool exhausted"}' \
  "$BASE/api/v1/incidents"
INCIDENT_ID="$(jsonfield "$LAST_BODY" incidentId)"
echo "      incidentId=$INCIDENT_ID"

check "AGENT_RUNTIME 创建 Run -> 201" 201 -X POST -H "$AG" -H "$JSON" \
  -d "{\"incidentId\":\"$INCIDENT_ID\",\"graphVersion\":\"g1\",\"promptVersion\":\"p1\",\"modelId\":\"fake-provider\",\"datasetVersion\":\"incidents-dev-v1\"}" \
  "$BASE/api/v1/runs"
RUN_ID="$(jsonfield "$LAST_BODY" runId)"
echo "      runId=$RUN_ID"

ARGS='{"service":"synthetic-orders","target_version":"v1.4.2"}'
check "发起审批请求 -> 201" 201 -X POST -H "$AG" -H "$JSON" \
  -d "{\"runId\":\"$RUN_ID\",\"toolName\":\"rollback_synthetic_deployment\",\"resourceRef\":\"svc:synthetic-orders\",\"arguments\":$ARGS}" \
  "$BASE/api/v1/approvals"
APPROVAL_ID="$(jsonfield "$LAST_BODY" approvalId)"
DIGEST="$(jsonfield "$LAST_BODY" argumentsDigest)"
echo "      approvalId=$APPROVAL_ID"
echo "      argumentsDigest=$DIGEST"

check "AGENT_RUNTIME 自批 -> 403（INV-4）" 403 -X POST -H "$AG" -H "$JSON" \
  -d '{"approve":true,"reason":"self approve attempt"}' \
  "$BASE/api/v1/approvals/$APPROVAL_ID/decision"

check "未批准即消费 -> 403" 403 -X POST -H "$AG" -H "$JSON" \
  -d "{\"toolName\":\"rollback_synthetic_deployment\",\"resourceRef\":\"svc:synthetic-orders\",\"arguments\":$ARGS}" \
  "$BASE/api/v1/approvals/$APPROVAL_ID/consume"

check "APPROVER 批准 -> 200" 200 -X POST -H "$AP" -H "$JSON" \
  -d '{"approve":true,"reason":"evidence chain checked"}' \
  "$BASE/api/v1/approvals/$APPROVAL_ID/decision"

echo
echo "== 威胁 T-2：审批后篡改参数 =="
check "改 service 后消费 -> 403" 403 -X POST -H "$AG" -H "$JSON" \
  -d '{"toolName":"rollback_synthetic_deployment","resourceRef":"svc:synthetic-orders","arguments":{"service":"synthetic-db","target_version":"v1.4.2"}}' \
  "$BASE/api/v1/approvals/$APPROVAL_ID/consume"

check "改 toolName 后消费 -> 403" 403 -X POST -H "$AG" -H "$JSON" \
  -d "{\"toolName\":\"restart_synthetic_service\",\"resourceRef\":\"svc:synthetic-orders\",\"arguments\":$ARGS}" \
  "$BASE/api/v1/approvals/$APPROVAL_ID/consume"

check "改 resourceRef 后消费 -> 403" 403 -X POST -H "$AG" -H "$JSON" \
  -d "{\"toolName\":\"rollback_synthetic_deployment\",\"resourceRef\":\"svc:synthetic-db\",\"arguments\":$ARGS}" \
  "$BASE/api/v1/approvals/$APPROVAL_ID/consume"

check "键顺序不同但语义相同 -> 200（不误拒）" 200 -X POST -H "$AG" -H "$JSON" \
  -d '{"toolName":"rollback_synthetic_deployment","resourceRef":"svc:synthetic-orders","arguments":{"target_version":"v1.4.2","service":"synthetic-orders"}}' \
  "$BASE/api/v1/approvals/$APPROVAL_ID/consume"

check "重放已消费的审批 -> 403" 403 -X POST -H "$AG" -H "$JSON" \
  -d "{\"toolName\":\"rollback_synthetic_deployment\",\"resourceRef\":\"svc:synthetic-orders\",\"arguments\":$ARGS}" \
  "$BASE/api/v1/approvals/$APPROVAL_ID/consume"

echo
echo "== ADR-0002：浮点参数被拒绝 =="
check "arguments 含浮点 -> 400" 400 -X POST -H "$AG" -H "$JSON" \
  -d "{\"runId\":\"$RUN_ID\",\"toolName\":\"throttle_synthetic_traffic\",\"resourceRef\":\"svc:synthetic-notify\",\"arguments\":{\"rate_percent\":12.5}}" \
  "$BASE/api/v1/approvals"

echo
echo "== 唯一终态（INV-5）：同一 Run 结算三次 =="
check "第一次结算 -> 200" 200 -X POST -H "$AG" -H "$JSON" \
  -d '{"terminalStatus":"COMPLETE"}' "$BASE/api/v1/runs/$RUN_ID/terminal"
FIRST_1="$(jsonfield "$LAST_BODY" firstSettlement)"
check "第二次结算（换成 FAILED）-> 200" 200 -X POST -H "$AG" -H "$JSON" \
  -d '{"terminalStatus":"FAILED","failureClass":"deadline_exceeded"}' \
  "$BASE/api/v1/runs/$RUN_ID/terminal"
FIRST_2="$(jsonfield "$LAST_BODY" firstSettlement)"
STATUS_2="$(jsonfield "$LAST_BODY" terminalStatus)"
check "第三次结算 -> 200" 200 -X POST -H "$AG" -H "$JSON" \
  -d '{"terminalStatus":"COMPLETE"}' "$BASE/api/v1/runs/$RUN_ID/terminal"
FIRST_3="$(jsonfield "$LAST_BODY" firstSettlement)"

if [ "$FIRST_1" = "True" ] && [ "$FIRST_2" = "False" ] && [ "$FIRST_3" = "False" ] \
   && [ "$STATUS_2" = "COMPLETE" ]; then
  echo "ok    三次结算只有第一次生效，终态未被改写"
  PASS=$((PASS + 1))
else
  echo "FAIL  终态幂等性: first=$FIRST_1/$FIRST_2/$FIRST_3 status2=$STATUS_2"
  FAIL=$((FAIL + 1))
fi

echo
echo "== 乐观锁 =="
check "创建 Incident 用于并发测试 -> 201" 201 -X POST -H "$OP" -H "$JSON" \
  -d '{"source":"synthetic-lab","severity":"P3","title":"optimistic lock probe"}' \
  "$BASE/api/v1/incidents"
LOCK_ID="$(jsonfield "$LAST_BODY" incidentId)"
check "version=1 更新 -> 200" 200 -X PATCH -H "$OP" -H "$JSON" \
  -d '{"status":"DIAGNOSING","expectedVersion":1}' \
  "$BASE/api/v1/incidents/$LOCK_ID/status"
check "重用 version=1 -> 409" 409 -X PATCH -H "$OP" -H "$JSON" \
  -d '{"status":"MITIGATING","expectedVersion":1}' \
  "$BASE/api/v1/incidents/$LOCK_ID/status"

echo
echo "== 审计 =="
check "审计事件可查" 200 -H "$OP" "$BASE/api/v1/audit-events?limit=100"
DENIED_COUNT="$(printf '%s' "$LAST_BODY" | python3 -c "import sys,json;print(sum(1 for e in json.load(sys.stdin) if e['outcome']=='DENIED'))")"
if [ "$DENIED_COUNT" -ge 5 ]; then
  # ${} 是必需的：紧跟其后的全角括号在 bash 3.2 下会被当成变量名的一部分。
  echo "ok    拒绝类审计事件已记录（DENIED=${DENIED_COUNT}）"
  PASS=$((PASS + 1))
else
  echo "FAIL  拒绝类审计事件过少：DENIED=${DENIED_COUNT}（期望 >= 5）"
  FAIL=$((FAIL + 1))
fi

echo
echo "== Secret 不泄漏 =="
LEAK="$(curl -s -H "Authorization: Bearer leaked-secret-token-abc123" "$BASE/api/v1/incidents")"
if printf '%s' "$LEAK" | grep -q 'leaked-secret-token-abc123'; then
  echo "FAIL  401 响应回显了 token：$LEAK"
  FAIL=$((FAIL + 1))
else
  echo "ok    401 响应未回显 token"
  PASS=$((PASS + 1))
fi

echo
echo "==============================="
echo "PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
