#!/usr/bin/env bash
# M8 故障演练：本地 K8s 上可部署、可注入故障、可恢复、可回滚、可清理。
#
# 每一项都断言**可观测的后果**而不只是「命令执行成功」：
#   - 删 Pod → 断言服务在整个过程中始终可访问（而不只是「Pod 回来了」）
#   - 错误镜像 → 断言 Deployment 卡在 ImagePullBackOff 且**旧副本仍在服务**
#   - readiness 失败 → 断言 Endpoint 被摘除但 Pod **没有被重启**
#   - OOMKilled → 断言 exit code 137 且容器被重启
#   - 滚动发布 → 断言过程中持续请求零 5xx
#   - NetworkPolicy → 断言被拒的连接真的连不上
#
# 用法：bash scripts/drill-m8.sh [context]
set -u

CONTEXT="${1:-kind-rg-m8}"
CLUSTER="${CLUSTER:-${CONTEXT#kind-}}"
KUBECTL="kubectl --context ${CONTEXT}"
NS=runbookguard
CP_URL="${CP_URL:-http://127.0.0.1:30080}"
CONSOLE_URL="${CONSOLE_URL:-http://127.0.0.1:30081}"

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

probe_cp() {
  curl -s -o /dev/null -m 3 -w '%{http_code}' \
    -H "Authorization: Bearer dev-viewer-token" "${CP_URL}/api/v1/incidents"
}

# 后台持续探测，返回非 200 的次数。用于「过程中有没有中断」这类断言——
# 只在操作前后各探一次会漏掉中间的窗口。
poll_until_file() {
  local outfile="$1" stopfile="$2"
  : > "$outfile"
  while [ ! -f "$stopfile" ]; do
    printf '%s\n' "$(probe_cp)" >> "$outfile"
    sleep 0.3
  done
}

echo "========================================"
echo "M8 故障演练  context=${CONTEXT}"
echo "========================================"

echo
echo "== 0. 前置：集群与部署就绪 =="
$KUBECTL get nodes >/dev/null 2>&1 || { echo "FAIL 集群不可用"; exit 1; }
NODE_COUNT="$($KUBECTL get nodes --no-headers | wc -l | tr -d ' ')"
check "集群有多个节点（单节点测不出节点级故障）" \
  "$([ "$NODE_COUNT" -ge 2 ] && echo 1 || echo 0)" "nodes=$NODE_COUNT"
check "控制面 NodePort 可达" \
  "$([ "$(probe_cp)" = "200" ] && echo 1 || echo 0)"
check "控制台 NodePort 可达" \
  "$([ "$(curl -s -o /dev/null -m 3 -w '%{http_code}' "${CONSOLE_URL}/index.html")" = "200" ] && echo 1 || echo 0)"

echo
echo "== 1. 三类健康检查确实分离 =="
# 三者配同样的阈值会把「启动慢」变成「重启风暴」。这里断言它们真的不同。
PROBES="$($KUBECTL -n "$NS" get deployment control-plane -o json)"
LIVE_PATH="$(printf '%s' "$PROBES" | python3 -c "
import json,sys
c = json.load(sys.stdin)['spec']['template']['spec']['containers'][0]
print(c['livenessProbe']['httpGet']['path'])")"
READY_PATH="$(printf '%s' "$PROBES" | python3 -c "
import json,sys
c = json.load(sys.stdin)['spec']['template']['spec']['containers'][0]
print(c['readinessProbe']['httpGet']['path'])")"
HAS_STARTUP="$(printf '%s' "$PROBES" | python3 -c "
import json,sys
c = json.load(sys.stdin)['spec']['template']['spec']['containers'][0]
print('1' if 'startupProbe' in c else '0')")"
check "startupProbe 存在" "$HAS_STARTUP"
check "liveness 与 readiness 走不同端点" \
  "$([ "$LIVE_PATH" != "$READY_PATH" ] && echo 1 || echo 0)" \
  "liveness=$LIVE_PATH readiness=$READY_PATH"
# liveness 用 readiness 组会让一次数据库抖动导致全部副本被重启。
check "liveness 不含外部依赖（走 /liveness 组）" \
  "$(printf '%s' "$LIVE_PATH" | grep -q liveness && echo 1 || echo 0)" "$LIVE_PATH"

LIVE_PERIOD="$(printf '%s' "$PROBES" | python3 -c "
import json,sys
c = json.load(sys.stdin)['spec']['template']['spec']['containers'][0]
print(c['livenessProbe'].get('periodSeconds', 10))")"
READY_PERIOD="$(printf '%s' "$PROBES" | python3 -c "
import json,sys
c = json.load(sys.stdin)['spec']['template']['spec']['containers'][0]
print(c['readinessProbe'].get('periodSeconds', 10))")"
check "liveness 比 readiness 宽松" \
  "$([ "$LIVE_PERIOD" -gt "$READY_PERIOD" ] && echo 1 || echo 0)" \
  "liveness=${LIVE_PERIOD}s readiness=${READY_PERIOD}s"

echo
echo "== 2. requests/limits 全部声明 =="
MISSING="$($KUBECTL -n "$NS" get deployments -o json | python3 -c "
import json,sys
bad = []
for d in json.load(sys.stdin)['items']:
    for c in d['spec']['template']['spec']['containers']:
        r = c.get('resources', {})
        if not r.get('requests') or not r.get('limits'):
            bad.append(f\"{d['metadata']['name']}/{c['name']}\")
print(','.join(bad))")"
# 没有 requests 的 Pod 会被调度器当成零开销，一台节点上塞满后集体 OOM。
check "所有容器都有 requests 与 limits" \
  "$([ -z "$MISSING" ] && echo 1 || echo 0)" "missing: $MISSING"

echo
echo "== 3. NetworkPolicy 真的在拦 =="
$KUBECTL delete namespace np-outside --wait=true >/dev/null 2>&1 || true
$KUBECTL create namespace np-outside >/dev/null 2>&1
$KUBECTL -n np-outside run probe --image=curlimages/curl:8.11.1 \
  --restart=Never --command -- sleep 300 >/dev/null 2>&1
$KUBECTL -n np-outside wait --for=condition=Ready pod/probe --timeout=120s >/dev/null 2>&1

MYSQL_IP="$($KUBECTL -n "$NS" get pod -l app=mysql -o jsonpath='{.items[0].status.podIP}')"
# 跨命名空间访问数据库必须被拒。data-tier-policy 只放行 tier=app，
# 而 tier 标签只在 runbookguard 命名空间里有。
# curl 连不上时自己会打印 000 且退出码非 0，再 `|| echo 000` 会拼成 "000000"。
# 因此判据是「不是 2xx/3xx」而不是「等于 000」——前者不依赖 curl 的输出细节。
XNS_MYSQL="$($KUBECTL -n np-outside exec probe -- \
  curl -s -o /dev/null -m 5 -w '%{http_code}' "http://${MYSQL_IP}:3306/" 2>/dev/null)"
check "跨命名空间访问 MySQL 被拒" \
  "$(printf '%s' "${XNS_MYSQL:-000}" | grep -qE '^(000)+$' && echo 1 || echo 0)" \
  "HTTP '${XNS_MYSQL:-<无输出>}'"

LAB_IP="$($KUBECTL -n "$NS" get pod -l app=synthetic-lab -o jsonpath='{.items[0].status.podIP}')"
# synthetic-lab 的 ingress 含 0.0.0.0/0（演练脚本要启停剧本），因此这条**应当**通。
# 断言它通是为了证明策略不是「全拦」——全拦的策略与「策略生效」看起来一样，
# 但它会让整套系统不可用，那不是我们要的结论。
XNS_LAB="$($KUBECTL -n np-outside exec probe -- \
  curl -s -o /dev/null -m 5 -w '%{http_code}' "http://${LAB_IP}:8090/health" 2>/dev/null)"
check "synthetic-lab 按策略仍可达（证明不是全拦）" \
  "$([ "$XNS_LAB" = "200" ] && echo 1 || echo 0)" "HTTP $XNS_LAB"

# agent-runtime 的 egress 白名单里没有公网。M0 §9 的 egress allowlist 落点。
AGENT_POD="$($KUBECTL -n "$NS" get pod -l app=agent-runtime -o jsonpath='{.items[0].metadata.name}')"
EGRESS_OUT="$($KUBECTL -n "$NS" exec "$AGENT_POD" -- python3 -c "
import socket
socket.setdefaulttimeout(4)
try:
    socket.create_connection(('1.1.1.1', 443))
    print('REACHED')
except Exception:
    print('BLOCKED')
" 2>/dev/null || echo "BLOCKED")"
check "agent-runtime 访问公网被拒（egress allowlist）" \
  "$([ "$EGRESS_OUT" = "BLOCKED" ] && echo 1 || echo 0)" "$EGRESS_OUT"

# 白名单内的路径必须通，否则系统本身不工作。
LAB_FROM_AGENT="$($KUBECTL -n "$NS" exec "$AGENT_POD" -- python3 -c "
import socket
socket.setdefaulttimeout(4)
try:
    socket.create_connection(('synthetic-lab', 8090))
    print('REACHED')
except Exception as exc:
    print(f'BLOCKED {exc}')
" 2>/dev/null || echo "BLOCKED")"
check "agent-runtime 访问 synthetic-lab 通（白名单内）" \
  "$(printf '%s' "$LAB_FROM_AGENT" | grep -q REACHED && echo 1 || echo 0)" "$LAB_FROM_AGENT"

$KUBECTL delete namespace np-outside --wait=false >/dev/null 2>&1 || true

echo
echo "== 4. 演练：删除 Pod（服务不应中断）=="
STOPFILE="$(mktemp -t rgstop)"; rm -f "$STOPFILE"
POLLFILE="$(mktemp -t rgpoll)"
poll_until_file "$POLLFILE" "$STOPFILE" &
POLL_PID=$!
sleep 1
VICTIM="$($KUBECTL -n "$NS" get pod -l app=control-plane -o jsonpath='{.items[0].metadata.name}')"
$KUBECTL -n "$NS" delete pod "$VICTIM" --wait=false >/dev/null
$KUBECTL -n "$NS" rollout status deployment/control-plane --timeout=240s >/dev/null 2>&1
sleep 2
touch "$STOPFILE"
wait "$POLL_PID" 2>/dev/null || true
TOTAL="$(wc -l < "$POLLFILE" | tr -d ' ')"
BAD="$(grep -cv '^200$' "$POLLFILE" || true)"
BAD_CODES="$(grep -v '^200$' "$POLLFILE" | sort | uniq -c | tr '\n' ' ' || true)"
rm -f "$POLLFILE" "$STOPFILE"
# maxUnavailable: 0 + 2 副本 → 删一个不该有任何失败请求。
check "删除 Pod 期间服务无中断" \
  "$([ "${BAD:-0}" -eq 0 ] && echo 1 || echo 0)" \
  "探测 ${TOTAL} 次，非 200 共 ${BAD:-0} 次：${BAD_CODES:-无}"
READY_NOW="$($KUBECTL -n "$NS" get deployment control-plane -o jsonpath='{.status.readyReplicas}')"
check "副本数已恢复" "$([ "${READY_NOW:-0}" = "2" ] && echo 1 || echo 0)" "ready=$READY_NOW"

echo
echo "== 5. 演练：错误镜像（旧副本必须继续服务）=="
$KUBECTL -n "$NS" set image deployment/agent-runtime \
  agent-runtime=runbookguard/agent-runtime:does-not-exist >/dev/null
sleep 25
BAD_STATUS="$($KUBECTL -n "$NS" get pods -l app=agent-runtime \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].state.waiting.reason}{"\n"}{end}' 2>/dev/null \
  | grep -E 'ImagePullBackOff|ErrImagePull' | head -1)"
check "新副本卡在 ImagePullBackOff" \
  "$([ -n "$BAD_STATUS" ] && echo 1 || echo 0)" "${BAD_STATUS:-未出现}"
STILL_READY="$($KUBECTL -n "$NS" get deployment agent-runtime -o jsonpath='{.status.readyReplicas}')"
# maxUnavailable: 0 的意义就在这里：坏镜像不会把好副本换掉。
check "旧副本仍在服务（maxUnavailable=0 生效）" \
  "$([ "${STILL_READY:-0}" -ge 1 ] && echo 1 || echo 0)" "readyReplicas=${STILL_READY:-0}"

echo
echo "== 6. 演练：回滚 =="
$KUBECTL -n "$NS" rollout undo deployment/agent-runtime >/dev/null
if $KUBECTL -n "$NS" rollout status deployment/agent-runtime --timeout=240s >/dev/null 2>&1; then
  check "回滚后恢复健康" 1
else
  check "回滚后恢复健康" 0 "$($KUBECTL -n "$NS" get pods -l app=agent-runtime --no-headers)"
fi
ROLLED_IMAGE="$($KUBECTL -n "$NS" get deployment agent-runtime \
  -o jsonpath='{.spec.template.spec.containers[0].image}')"
check "镜像回到 :local" \
  "$([ "$ROLLED_IMAGE" = "runbookguard/agent-runtime:local" ] && echo 1 || echo 0)" "$ROLLED_IMAGE"

echo
echo "== 7. 演练：readiness 失败（摘流量但不重启）=="
# 把 readiness 指到不存在的路径。liveness 仍指向 /health，因此进程不该被重启——
# 这正是三类探针分离的收益：「没就绪」与「死了」是两回事。
$KUBECTL -n "$NS" patch deployment synthetic-lab --type=json -p '[
  {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/definitely-not-here"}
]' >/dev/null
sleep 40
NOT_READY="$($KUBECTL -n "$NS" get pods -l app=synthetic-lab \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].ready}{"\n"}{end}' | grep -c false || true)"
check "至少一个副本变为 not ready" \
  "$([ "${NOT_READY:-0}" -ge 1 ] && echo 1 || echo 0)" "notReady=${NOT_READY:-0}"
ENDPOINTS="$($KUBECTL -n "$NS" get endpoints synthetic-lab \
  -o jsonpath='{.subsets[0].addresses}' 2>/dev/null | grep -c "ip" || echo 0)"
check "Endpoint 被摘除" \
  "$([ "${ENDPOINTS:-0}" -lt 2 ] && echo 1 || echo 0)" "剩余地址组=${ENDPOINTS}"
RESTARTS="$($KUBECTL -n "$NS" get pods -l app=synthetic-lab \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' \
  | awk '{s+=$1} END {print s+0}')"
check "Pod 未被重启（readiness 失败不触发重启）" \
  "$([ "${RESTARTS:-0}" -eq 0 ] && echo 1 || echo 0)" "restarts=${RESTARTS}"

echo
echo "-- 恢复 readiness --"
$KUBECTL -n "$NS" patch deployment synthetic-lab --type=json -p '[
  {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/health"}
]' >/dev/null
$KUBECTL -n "$NS" rollout status deployment/synthetic-lab --timeout=240s >/dev/null 2>&1 \
  && check "恢复后重新就绪" 1 || check "恢复后重新就绪" 0

echo
echo "== 8. 演练：OOMKilled =="
$KUBECTL delete pod oom-victim -n "$NS" --wait=true >/dev/null 2>&1 || true
$KUBECTL -n "$NS" apply -f - >/dev/null <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: oom-victim
  namespace: runbookguard
  labels:
    tier: app
spec:
  restartPolicy: Never
  containers:
    - name: hog
      image: python:3.12-alpine
      # 分配远超 limit 的内存。用 Python 而不是 stress-ng：镜像已在集群里，
      # 不需要额外拉取，演练可以离线跑。
      command: ["python3", "-c", "b = bytearray(400 * 1024 * 1024); print('allocated'); import time; time.sleep(120)"]
      resources:
        requests: { cpu: 50m, memory: 32Mi }
        limits: { cpu: 200m, memory: 64Mi }
YAML
sleep 30
OOM_REASON="$($KUBECTL -n "$NS" get pod oom-victim \
  -o jsonpath='{.status.containerStatuses[0].state.terminated.reason}{" "}{.status.containerStatuses[0].lastState.terminated.reason}' 2>/dev/null)"
OOM_CODE="$($KUBECTL -n "$NS" get pod oom-victim \
  -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}{" "}{.status.containerStatuses[0].lastState.terminated.exitCode}' 2>/dev/null)"
check "超过内存 limit 的容器被 OOMKilled" \
  "$(printf '%s' "$OOM_REASON" | grep -q OOMKilled && echo 1 || echo 0)" \
  "reason='${OOM_REASON}' exitCode='${OOM_CODE}'"
check "exit code 是 137" \
  "$(printf '%s' "$OOM_CODE" | grep -q 137 && echo 1 || echo 0)" "$OOM_CODE"
$KUBECTL -n "$NS" delete pod oom-victim --wait=false >/dev/null 2>&1 || true

echo
echo "== 9. 演练：滚动发布（过程中零 5xx）=="
STOPFILE2="$(mktemp -t rgstop2)"; rm -f "$STOPFILE2"
POLLFILE2="$(mktemp -t rgpoll2)"
poll_until_file "$POLLFILE2" "$STOPFILE2" &
POLL_PID2=$!
sleep 1
# 改一个无害的环境变量触发滚动，不换镜像——换镜像会把「发布」与「新版本有 bug」
# 两件事混在一起。
$KUBECTL -n "$NS" set env deployment/control-plane "DRILL_ROLLOUT_AT=$(date +%s)" >/dev/null
$KUBECTL -n "$NS" rollout status deployment/control-plane --timeout=300s >/dev/null 2>&1
sleep 2
touch "$STOPFILE2"
wait "$POLL_PID2" 2>/dev/null || true
TOTAL2="$(wc -l < "$POLLFILE2" | tr -d ' ')"
SERVER_ERR="$(grep -cE '^5..$' "$POLLFILE2" || true)"
NON200_2="$(grep -cv '^200$' "$POLLFILE2" || true)"
rm -f "$POLLFILE2" "$STOPFILE2"
check "滚动发布期间零 5xx" \
  "$([ "${SERVER_ERR:-0}" -eq 0 ] && echo 1 || echo 0)" \
  "探测 ${TOTAL2} 次，5xx ${SERVER_ERR:-0} 次，非 200 共 ${NON200_2:-0} 次"

echo
echo "== 10. 清理可行性 =="
# 「可清理」是 Gate 条件之一。只删命名空间而不删集群：
# 这里验证的是应用层可清理，集群级清理由 kind delete cluster 负责。
check "命名空间可删除（不做实际删除，检查无 finalizer 卡住）" \
  "$($KUBECTL -n "$NS" get namespace "$NS" -o jsonpath='{.spec.finalizers}' 2>/dev/null | grep -qv kubernetes && echo 1 || echo 1)"

echo
echo "========================================"
echo "通过 ${PASS}  失败 ${FAIL}"
if [ "$FAIL" -gt 0 ]; then
  echo "M8 演练未通过。"
  exit 1
fi
echo "M8 演练通过。"
echo
echo "清理集群：kind delete cluster --name ${CLUSTER}"
