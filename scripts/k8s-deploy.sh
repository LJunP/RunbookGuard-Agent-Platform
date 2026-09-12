#!/usr/bin/env bash
# 把本地镜像装进 kind 集群并部署。
#
# 为什么要 `kind load`：kind 节点是独立的 containerd，看不到宿主机 Docker 的镜像。
# 不 load 就会 ImagePullBackOff，而错误信息是「拉不到镜像」——很容易被误判成
# 网络问题或 tag 写错。
#
# 用法：bash scripts/k8s-deploy.sh [context]
set -u

CONTEXT="${1:-kind-rg-m8}"
CLUSTER="${CLUSTER:-${CONTEXT#kind-}}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBECTL="kubectl --context ${CONTEXT}"
NS=runbookguard

FAILED=0

echo "== 1. 确认集群可用 =="
if ! $KUBECTL get nodes >/dev/null 2>&1; then
  echo "FAIL  集群 ${CONTEXT} 不可用。先建集群："
  echo "      kind create cluster --config deploy/k8s/kind-cluster.yaml --image kindest/node:v1.34.0"
  exit 1
fi
$KUBECTL get nodes --no-headers | awk '{printf "      %-24s %s\n", $1, $2}'

echo
echo "== 2. 构建镜像（复用 compose 的 Dockerfile）=="
# 复用 compose 的构建定义而不是另写一套：两份 Dockerfile 会漂移，
# 而漂移的症状是「compose 里好的，k8s 里不好」，排查成本很高。
docker compose -f "${REPO_ROOT}/deploy/compose/docker-compose.yml" build \
  control-plane agent-runtime synthetic-lab console >/dev/null 2>&1 || {
  echo "FAIL  镜像构建失败"
  exit 1
}

declare -a PAIRS=(
  "compose-control-plane:latest runbookguard/control-plane:local"
  "compose-agent-runtime:latest runbookguard/agent-runtime:local"
  "compose-synthetic-lab:latest runbookguard/synthetic-lab:local"
  "compose-console:latest runbookguard/console:local"
)
for pair in "${PAIRS[@]}"; do
  src="${pair%% *}"
  dst="${pair##* }"
  docker tag "$src" "$dst" || { echo "FAIL  tag $src -> $dst"; FAILED=1; }
  printf '      %-34s -> %s\n' "$src" "$dst"
done
[ "$FAILED" -eq 0 ] || exit 1

echo
echo "== 3. 装载镜像到 kind 节点 =="
for pair in "${PAIRS[@]}"; do
  dst="${pair##* }"
  printf '      loading %-38s ' "$dst"
  if kind load docker-image "$dst" --name "$CLUSTER" >/dev/null 2>&1; then
    echo "ok"
  else
    echo "FAIL"
    FAILED=1
  fi
done
[ "$FAILED" -eq 0 ] || exit 1

echo
echo "== 4. apply manifests =="
$KUBECTL apply -f "${REPO_ROOT}/deploy/k8s/base/" || exit 1

echo
echo "== 5. 等中间件就绪（首次启动 MySQL 要初始化数据目录）=="
for dep in mysql redis rabbitmq; do
  printf '      %-12s ' "$dep"
  if $KUBECTL -n "$NS" rollout status "deployment/${dep}" --timeout=300s >/dev/null 2>&1; then
    echo "ready"
  else
    echo "TIMEOUT"
    FAILED=1
  fi
done

echo
echo "== 6. 等应用就绪 =="
for dep in synthetic-lab control-plane agent-runtime console; do
  printf '      %-16s ' "$dep"
  if $KUBECTL -n "$NS" rollout status "deployment/${dep}" --timeout=300s >/dev/null 2>&1; then
    echo "ready"
  else
    echo "TIMEOUT"
    $KUBECTL -n "$NS" get pods -l "app=${dep}" -o wide
    $KUBECTL -n "$NS" describe pods -l "app=${dep}" | grep -A 6 "Events:" | tail -12
    FAILED=1
  fi
done

echo
$KUBECTL -n "$NS" get pods -o wide

echo
if [ "$FAILED" -gt 0 ]; then
  echo "FAIL  部署未完成"
  exit 1
fi
cat <<MSG

ok    部署完成。

      控制面   http://127.0.0.1:30080/api/v1/incidents  （无凭据 401 即正常）
      管理口   Pod 内 9080（未映射宿主机；actuator 不对外）
      控制台   http://127.0.0.1:30081

      演练：bash scripts/drill-m8.sh ${CONTEXT}
      清理：kind delete cluster --name ${CLUSTER}
MSG
