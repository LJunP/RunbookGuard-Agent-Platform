#!/usr/bin/env bash
# M2 故障演练：Lease 接管、fencing token、幂等、唯一终态。
# 与集成测试互补——这里验证的是"打包成镜像、连真实 RabbitMQ 起来之后仍然对"。
#
# 前置：deploy/compose 已 up 且 control-plane 健康。
set -u

BASE="${BASE:-http://127.0.0.1:8080}"
OP="Authorization: Bearer dev-operator-token"
AG="Authorization: Bearer dev-agent-token"
JSON="Content-Type: application/json"

PASS=0
FAIL=0

check() {
  local label="$1" expected="$2"; shift 2
  local out status body
  out="$(curl -s -w '\n%{http_code}' "$@")"
  status="$(printf '%s' "$out" | tail -1)"
  body="$(printf '%s' "$out" | sed '$d')"
  if [ "$status" = "$expected" ]; then
    printf 'ok    %-52s [%s]\n' "$label" "$status"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %-52s expected %s got %s\n      %s\n' "$label" "$expected" "$status" "$body"
    FAIL=$((FAIL + 1))
  fi
  LAST_BODY="$body"
}

expect() {
  local label="$1" actual="$2" want="$3"
  if [ "$actual" = "$want" ]; then
    printf 'ok    %-52s [%s]\n' "$label" "$actual"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %-52s expected %s got %s\n' "$label" "$want" "$actual"
    FAIL=$((FAIL + 1))
  fi
}

field() {
  printf '%s' "$1" | python3 -c "import sys,json;print(json.load(sys.stdin)[sys.argv[1]])" "$2"
}

new_run() {
  local body
  body="$(curl -s -X POST -H "$OP" -H "$JSON" \
    -d '{"source":"synthetic-lab","severity":"P2","title":"m2 drill"}' \
    "$BASE/api/v1/incidents")"
  local incident_id
  incident_id="$(field "$body" incidentId)"
  body="$(curl -s -X POST -H "$AG" -H "$JSON" \
    -d "{\"incidentId\":\"$incident_id\",\"graphVersion\":\"g1\",\"promptVersion\":\"p1\",\"modelId\":\"fake-provider\",\"datasetVersion\":\"incidents-dev-v1\"}" \
    "$BASE/api/v1/runs")"
  field "$body" runId
}

echo "== 演练 1：Lease 获取与互斥 =="
RUN1="$(new_run)"
echo "      runId=$RUN1"

check "worker-1 获取 Lease -> 200" 200 -X POST -H "$AG" -H "$JSON" \
  -d '{"workerId":"worker-1"}' "$BASE/api/v1/worker/runs/$RUN1/lease"
TOKEN1="$(field "$LAST_BODY" fencingToken)"
expect "acquired=true" "$(field "$LAST_BODY" acquired)" "True"
expect "fencingToken=1" "$TOKEN1" "1"

check "worker-2 在有效期内获取 -> 200" 200 -X POST -H "$AG" -H "$JSON" \
  -d '{"workerId":"worker-2"}' "$BASE/api/v1/worker/runs/$RUN1/lease"
expect "worker-2 acquired=false（互斥生效）" "$(field "$LAST_BODY" acquired)" "False"

echo
echo "== 演练 2：进度上报需要有效 fencing token =="
check "worker-1 用有效 token 上报 -> 200" 200 -X POST -H "$AG" -H "$JSON" \
  -d "{\"workerId\":\"worker-1\",\"fencingToken\":$TOKEN1,\"status\":\"OBSERVE\",\"currentStep\":\"OBSERVE\",\"costSpentMicros\":1500,\"tokenSpent\":900,\"toolCallCount\":2,\"stepsUsed\":3}" \
  "$BASE/api/v1/worker/runs/$RUN1/progress"
expect "预算消耗已记录 costSpentMicros=1500" "$(field "$LAST_BODY" costSpentMicros)" "1500"
expect "stepsUsed=3" "$(field "$LAST_BODY" stepsUsed)" "3"

check "worker-2 用伪造 token 上报 -> 409" 409 -X POST -H "$AG" -H "$JSON" \
  -d '{"workerId":"worker-2","fencingToken":999,"status":"OBSERVE","costSpentMicros":0,"tokenSpent":0,"toolCallCount":0,"stepsUsed":0}' \
  "$BASE/api/v1/worker/runs/$RUN1/progress"

echo
echo "== 演练 3：kill -9 模拟（Lease 过期后接管，僵尸 Worker 被 fencing 拦住）=="
echo "      Lease TTL 默认 30s，等待过期..."
check "worker-1 主动释放 Lease（等价于优雅退出）" 200 -X POST -H "$AG" -H "$JSON" \
  -d "{\"workerId\":\"worker-1\",\"fencingToken\":$TOKEN1}" \
  "$BASE/api/v1/worker/runs/$RUN1/lease/release"

check "worker-2 接管 -> 200" 200 -X POST -H "$AG" -H "$JSON" \
  -d '{"workerId":"worker-2"}' "$BASE/api/v1/worker/runs/$RUN1/lease"
TOKEN2="$(field "$LAST_BODY" fencingToken)"
expect "接管成功 acquired=true" "$(field "$LAST_BODY" acquired)" "True"
if [ "$TOKEN2" -gt "$TOKEN1" ]; then
  # ${} 是必需的：紧跟其后的全角括号在 bash 3.2 下会被并入变量名。
  echo "ok    fencing token 递增（${TOKEN1} -> ${TOKEN2}）"
  PASS=$((PASS + 1))
else
  echo "FAIL  fencing token 未递增：${TOKEN1} -> ${TOKEN2}"
  FAIL=$((FAIL + 1))
fi

check "僵尸 worker-1 带旧 token 上报 -> 409" 409 -X POST -H "$AG" -H "$JSON" \
  -d "{\"workerId\":\"worker-1\",\"fencingToken\":$TOKEN1,\"status\":\"VERIFY\",\"costSpentMicros\":9999,\"tokenSpent\":9999,\"toolCallCount\":9,\"stepsUsed\":9}" \
  "$BASE/api/v1/worker/runs/$RUN1/progress"

check "僵尸 worker-1 heartbeat -> 200 但 acquired=false" 200 -X POST -H "$AG" -H "$JSON" \
  -d "{\"workerId\":\"worker-1\",\"fencingToken\":$TOKEN1}" \
  "$BASE/api/v1/worker/runs/$RUN1/lease/heartbeat"
expect "heartbeat 明确告知已失去 Lease" "$(field "$LAST_BODY" acquired)" "False"

echo
echo "== 演练 4：唯一终态（接管者结算三次）=="
TERMINAL_BODY="{\"workerId\":\"worker-2\",\"fencingToken\":$TOKEN2,\"terminalStatus\":\"COMPLETE\"}"
check "第一次结算 -> 200" 200 -X POST -H "$AG" -H "$JSON" -d "$TERMINAL_BODY" \
  "$BASE/api/v1/worker/runs/$RUN1/terminal"
expect "firstSettlement=true" "$(field "$LAST_BODY" firstSettlement)" "True"

check "第二次结算（改成 FAILED）-> 200" 200 -X POST -H "$AG" -H "$JSON" \
  -d "{\"workerId\":\"worker-2\",\"fencingToken\":$TOKEN2,\"terminalStatus\":\"FAILED\",\"failureClass\":\"deadline_exceeded\"}" \
  "$BASE/api/v1/worker/runs/$RUN1/terminal"
expect "firstSettlement=false" "$(field "$LAST_BODY" firstSettlement)" "False"
expect "终态未被改写，仍是 COMPLETE" "$(field "$LAST_BODY" terminalStatus)" "COMPLETE"

check "第三次结算 -> 200" 200 -X POST -H "$AG" -H "$JSON" -d "$TERMINAL_BODY" \
  "$BASE/api/v1/worker/runs/$RUN1/terminal"
expect "仍为 firstSettlement=false" "$(field "$LAST_BODY" firstSettlement)" "False"

check "终态后继续上报进度 -> 409" 409 -X POST -H "$AG" -H "$JSON" \
  -d "{\"workerId\":\"worker-2\",\"fencingToken\":$TOKEN2,\"status\":\"OBSERVE\",\"costSpentMicros\":0,\"tokenSpent\":0,\"toolCallCount\":0,\"stepsUsed\":0}" \
  "$BASE/api/v1/worker/runs/$RUN1/progress"

echo
echo "== 演练 5：failureClass 受控枚举 =="
RUN2="$(new_run)"
curl -s -X POST -H "$AG" -H "$JSON" -d '{"workerId":"w"}' \
  "$BASE/api/v1/worker/runs/$RUN2/lease" > /dev/null
TOKEN_R2="$(curl -s -X POST -H "$AG" -H "$JSON" -d '{"workerId":"w"}' \
  "$BASE/api/v1/worker/runs/$RUN2/lease" | python3 -c "import sys,json;print(json.load(sys.stdin)['fencingToken'])")"

check "自由文本 failureClass -> 400" 400 -X POST -H "$AG" -H "$JSON" \
  -d "{\"workerId\":\"w\",\"fencingToken\":$TOKEN_R2,\"terminalStatus\":\"FAILED\",\"failureClass\":\"it_broke_somehow\"}" \
  "$BASE/api/v1/worker/runs/$RUN2/terminal"

check "枚举内的 failureClass -> 200" 200 -X POST -H "$AG" -H "$JSON" \
  -d "{\"workerId\":\"w\",\"fencingToken\":$TOKEN_R2,\"terminalStatus\":\"FAILED\",\"failureClass\":\"insufficient_evidence\"}" \
  "$BASE/api/v1/worker/runs/$RUN2/terminal"

echo
echo "== 演练 6：取消传播 =="
RUN3="$(new_run)"
check "OPERATOR 请求取消 -> 200" 200 -X POST -H "$OP" "$BASE/api/v1/worker/runs/$RUN3/cancel"
expect "firstRequest=true" "$(field "$LAST_BODY" firstRequest)" "True"

check "重复取消 -> 200" 200 -X POST -H "$OP" "$BASE/api/v1/worker/runs/$RUN3/cancel"
expect "firstRequest=false（幂等）" "$(field "$LAST_BODY" firstRequest)" "False"

check "AGENT_RUNTIME 请求取消 -> 403" 403 -X POST -H "$AG" "$BASE/api/v1/worker/runs/$RUN3/cancel"

check "Worker 获取 Lease 时看到取消标记 -> 200" 200 -X POST -H "$AG" -H "$JSON" \
  -d '{"workerId":"w"}' "$BASE/api/v1/worker/runs/$RUN3/lease"
expect "cancelRequested=true" "$(field "$LAST_BODY" cancelRequested)" "True"

echo
echo "== 演练 7：审计留痕 =="
check "审计事件可查" 200 -H "$OP" "$BASE/api/v1/audit-events?limit=200"
LEASE_EVENTS="$(printf '%s' "$LAST_BODY" | python3 -c "import sys,json;print(sum(1 for e in json.load(sys.stdin) if e['action']=='lease.acquire'))")"
if [ "$LEASE_EVENTS" -ge 3 ]; then
  echo "ok    Lease 获取已留审计（lease.acquire=${LEASE_EVENTS}）"
  PASS=$((PASS + 1))
else
  echo "FAIL  Lease 审计过少：lease.acquire=${LEASE_EVENTS}"
  FAIL=$((FAIL + 1))
fi

echo
echo "==============================="
echo "PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
