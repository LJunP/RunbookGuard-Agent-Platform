#!/usr/bin/env bash
# 核验 kind 默认 CNI（kindnet）是否**真的执行** NetworkPolicy。
#
# 为什么必须实测而不是查文档：kindnetd 的 README 只列了三项职责
# （IP masquerade、netlink 路由、写 CNI 配置），既没说支持 NetworkPolicy
# 也没说不支持。「文档没提」不等于「不支持」，也不等于「支持」。
#
# 这件事的后果很具体：如果 kindnet 不执行 NetworkPolicy，那么
# 「apply 了 NetworkPolicy 且 kubectl 显示它存在」这个现象与
# 「策略真的在生效」是两个不同的事实。把前者写成后者，说明书 §20 的
# egress allowlist 要求就变成了一句无法验证的话。
#
# 做法：建一个 deny-all ingress 的 NetworkPolicy，然后从另一个 Pod 去连它。
# 连得上 → 策略没被执行。连不上 → 被执行了。
#
# 用法：
#   bash scripts/verify-networkpolicy-enforcement.sh [context]
set -u

CONTEXT="${1:-kind-rg-m8}"
NS="np-probe-$$"
KUBECTL="kubectl --context ${CONTEXT}"

cleanup() {
  $KUBECTL delete namespace "$NS" --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== 核验 NetworkPolicy 是否被执行 =="
echo "context: ${CONTEXT}"
echo

CNI="$($KUBECTL -n kube-system get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
  | grep -Ei 'kindnet|calico|cilium|flannel|weave' | head -1)"
echo "检测到的 CNI Pod：${CNI:-未识别}"

$KUBECTL create namespace "$NS" >/dev/null 2>&1 || {
  echo "FAIL  无法创建 namespace，集群可能不可用"
  exit 1
}

echo
echo "-- 起一个 target（nginx）与一个 client（curl）--"
$KUBECTL -n "$NS" apply -f - >/dev/null <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: target
  labels:
    app: target
spec:
  containers:
    - name: nginx
      image: nginxinc/nginx-unprivileged:1.27-alpine
      ports:
        - containerPort: 8080
---
apiVersion: v1
kind: Pod
metadata:
  name: client
  labels:
    app: client
spec:
  containers:
    - name: shell
      image: curlimages/curl:8.11.1
      command: ["sleep", "600"]
YAML

$KUBECTL -n "$NS" wait --for=condition=Ready pod/target pod/client --timeout=180s >/dev/null 2>&1 || {
  echo "FAIL  Pod 未就绪"
  $KUBECTL -n "$NS" get pods
  exit 1
}
TARGET_IP="$($KUBECTL -n "$NS" get pod target -o jsonpath='{.status.podIP}')"
echo "target IP: ${TARGET_IP}"

probe() {
  $KUBECTL -n "$NS" exec client -- \
    curl -s -o /dev/null -m 5 -w '%{http_code}' "http://${TARGET_IP}:8080/" 2>/dev/null
}

echo
echo "-- 基线：无策略时应当连得上 --"
BEFORE="$(probe)"
echo "HTTP ${BEFORE:-<无响应>}"
if [ "${BEFORE:-}" != "200" ]; then
  echo "FAIL  基线连通性就不成立，后续判定无意义（可能是镜像拉取或 CNI 本身的问题）"
  exit 1
fi
echo "ok    基线连通"

echo
echo "-- 施加 deny-all ingress 策略 --"
$KUBECTL -n "$NS" apply -f - >/dev/null <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-all-ingress
spec:
  # 空 podSelector = 命名空间内所有 Pod。
  podSelector: {}
  policyTypes:
    - Ingress
  # 没有 ingress 规则 = 拒绝全部入站。
YAML
$KUBECTL -n "$NS" get networkpolicy deny-all-ingress >/dev/null 2>&1 \
  && echo "ok    策略对象已创建（注意：对象存在 ≠ 策略生效）"

# 给 CNI 一点时间下发规则。
sleep 8

echo
echo "-- 施加策略后再探测 --"
AFTER="$(probe)"
echo "HTTP ${AFTER:-<无响应/超时>}"

echo
echo "========================================"
if [ "${AFTER:-}" = "200" ]; then
  cat <<'MSG'
结论：NetworkPolicy **未被执行**。

deny-all ingress 已 apply 且对象存在，但连接仍然成功。这说明当前 CNI
只提供连通性，不执行策略。

后果：说明书 §20 的 NetworkPolicy / egress allowlist 要求在这个集群上
**无法被验证**。manifest 里的 NetworkPolicy 是「声明了意图」而不是
「约束已生效」，Gate 报告必须这样写，不能写成「网络隔离已验证」。

可选做法（按代价从低到高）：
  1. 如实标注 UNKNOWN，manifest 保留 NetworkPolicy 供支持策略的集群使用
  2. kind + disableDefaultCNI: true，自行装 Calico（kind 文档称其为
     "power user feature with limited support"）
  3. 换 k3d（k3s 默认 flannel + 可选 network policy controller）
MSG
  exit 2
else
  cat <<'MSG'
结论：NetworkPolicy **被执行**。

施加 deny-all ingress 后连接失败，说明当前 CNI 真的在拦。
说明书 §20 的网络隔离要求可以在这个集群上被验证。
MSG
  exit 0
fi
