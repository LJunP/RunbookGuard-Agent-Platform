#!/usr/bin/env bash
# 等 compose 全栈就绪。
#
# 为什么不直接靠 `docker compose up --wait`：它只看 healthcheck，而 Prometheus、
# Grafana、collector 三个服务刻意没配 healthcheck（它们的就绪判定要打自己的 API，
# 写进 compose 会让 healthcheck 里塞进业务逻辑）。这个脚本用 HTTP 探测统一处理。
set -u

CONTROL_PLANE="${CONTROL_PLANE:-http://127.0.0.1:8080}"
# actuator 在独立管理端口（M7 §7.5 整改）。就绪探测必须走它——
# 业务端口上已经没有 /actuator 了。
MANAGEMENT="${MANAGEMENT:-http://127.0.0.1:9080}"
AGENT_RUNTIME="${AGENT_RUNTIME:-http://127.0.0.1:8100}"
SYNTHETIC_LAB="${SYNTHETIC_LAB:-http://127.0.0.1:8090}"
CONSOLE="${CONSOLE:-http://127.0.0.1:8081}"
PROMETHEUS="${PROMETHEUS:-http://127.0.0.1:9090}"
GRAFANA="${GRAFANA:-http://127.0.0.1:3000}"
TIMEOUT="${TIMEOUT:-180}"

FAILED=0

wait_for() {
  local name="$1" url="$2" deadline
  deadline=$(( $(date +%s) + TIMEOUT ))
  printf '%-16s %s ' "$name" "$url"
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -sf -o /dev/null "$url"; then
      echo "ok"
      return 0
    fi
    printf '.'
    sleep 2
  done
  echo "TIMEOUT after ${TIMEOUT}s"
  FAILED=$((FAILED + 1))
  return 1
}

echo "== 等待全栈就绪（每项最多 ${TIMEOUT}s）=="
wait_for "control-plane" "${MANAGEMENT}/actuator/health/readiness"
wait_for "agent-runtime" "${AGENT_RUNTIME}/health"
wait_for "synthetic-lab" "${SYNTHETIC_LAB}/health"
wait_for "console" "${CONSOLE}/index.html"
wait_for "prometheus" "${PROMETHEUS}/-/ready"
wait_for "grafana" "${GRAFANA}/api/health"

echo
if [ "$FAILED" -gt 0 ]; then
  echo "FAIL  ${FAILED} 个服务未就绪"
  exit 1
fi
echo "ok    全部服务已就绪"
