# M8 Gate 自检报告：Agent Infra

> 里程碑：M8（Agent Infra）
> 状态：**Gate 通过**（kind 集群部署 + 26 项故障演练全过 + NetworkPolicy 实测生效）
> 日期：2026-08-30
> 上游依据：项目说明书 §20 / §21，DEV_PROMPT §12 M8

---

## 0. 一句话结论

kind 三节点集群上部署 9 个 Pod，`scripts/drill-m8.sh` **26 项全过**：
三类健康检查确实分离、NetworkPolicy 实测在拦（含 agent-runtime 访问公网被拒）、
删 Pod 与滚动发布期间零失败请求、错误镜像不换掉好副本、readiness 失败摘流量但不重启、
OOMKilled 拿到 exit code 137、回滚可用。

M0 §9 的动作工具进程隔离**已落地**（受限子进程，ADR-0010），
挂载隔离与只读根文件系统仍是缺口——需要容器才能做到，见 §7.2。

---

## 1. 关键核验：kindnet 是否执行 NetworkPolicy

这是 M8 最需要先回答的问题。M6/M7 报告里把它列为「未核验」，因为它决定了
说明书 §20 的网络隔离要求**能不能被验证**。

kindnetd 的 README 只列了三项职责（IP masquerade、netlink 路由、写 CNI 配置），
**既没说支持也没说不支持** NetworkPolicy。`kind` 的配置文档完全没提这个词。
因此只能实测。

`scripts/verify-networkpolicy-enforcement.sh` 的真实输出：

```
== 核验 NetworkPolicy 是否被执行 ==
context: kind-rg-m8

检测到的 CNI Pod：kindnet-qkhpd
target IP: 10.244.0.5

-- 基线：无策略时应当连得上 --
HTTP 200
ok    基线连通

-- 施加 deny-all ingress 策略 --
ok    策略对象已创建（注意：对象存在 ≠ 策略生效）

-- 施加策略后再探测 --
HTTP 000

========================================
结论：NetworkPolicy **被执行**。
```

镜像版本：`docker.io/kindest/kindnetd:v20250512-df8de77b`。
它的日志里可见 `Syncing nftables rules`。

**因此不换 Calico。** kind 文档把 `disableDefaultCNI` 标注为
"power user feature with limited support"，在策略已能执行的前提下换掉它
只会引入一个不必要的变量。

这个脚本被放进 CI 的 k8s job 并且**排在部署之前**：如果 CNI 不执行策略，
后面所有「被拒绝」的断言都会变成假通过。

---

## 2. 交付物清单

### K8s manifests（`deploy/k8s/`）

| 文件 | 内容 |
|---|---|
| `kind-cluster.yaml` | 三节点（1 control-plane + 2 worker）+ NodePort 映射 |
| `base/00-namespace.yaml` | Namespace / ConfigMap / Secret |
| `base/10-middleware.yaml` | MySQL / Redis / RabbitMQ |
| `base/20-apps.yaml` | 四个应用，三类探针分离 + requests/limits + preStop |
| `base/30-networkpolicy.yaml` | default-deny-all + 6 条逐服务白名单 |

### 动作沙箱（`apps/agent-runtime-python/src/agent_runtime/tools/`）

| 文件 | 内容 |
|---|---|
| `action_sandbox.py` | 子进程启动、rlimit 规格、环境白名单、强制取消、限制校验 |
| `action_runner.py` | 子进程入口。不导入任何业务模块 |
| `action_executor.py` | `sandbox=True` 为默认；两条路径的输出整形共用 |

### 脚本

| 脚本 | 用途 |
|---|---|
| `scripts/verify-networkpolicy-enforcement.sh` | 核验 CNI 是否真的执行策略 |
| `scripts/k8s-deploy.sh` | 构建 + `kind load` + apply + 等就绪 |
| `scripts/drill-m8.sh` | 26 项故障演练 |

### 文档

| 文档 | 内容 |
|---|---|
| `docs/adr/ADR-0010-action-sandbox-and-k8s-boundary.md` | 隔离边界逐条对照，含未实现项 |
| `docs/architecture/M8-model-gateway-and-vllm-boundary.md` | 三层职责、客户端契约、**无任何性能验证** |

### CI

新增 `k8s` job：建 kind 集群 → 核验 CNI → 部署 → 演练 → 失败时导出集群状态。
CI 现有 6 条 job。

---

## 3. 实际运行的测试与真实输出

### 3.1 故障演练（26 项）

```
$ bash scripts/drill-m8.sh kind-rg-m8

== 0. 前置：集群与部署就绪 ==
ok    集群有多个节点（单节点测不出节点级故障）
ok    控制面 NodePort 可达
ok    控制台 NodePort 可达

== 1. 三类健康检查确实分离 ==
ok    startupProbe 存在
ok    liveness 与 readiness 走不同端点
ok    liveness 不含外部依赖（走 /liveness 组）
ok    liveness 比 readiness 宽松

== 2. requests/limits 全部声明 ==
ok    所有容器都有 requests 与 limits

== 3. NetworkPolicy 真的在拦 ==
ok    跨命名空间访问 MySQL 被拒
ok    synthetic-lab 按策略仍可达（证明不是全拦）
ok    agent-runtime 访问公网被拒（egress allowlist）
ok    agent-runtime 访问 synthetic-lab 通（白名单内）

== 4. 演练：删除 Pod（服务不应中断）==
ok    删除 Pod 期间服务无中断
ok    副本数已恢复

== 5. 演练：错误镜像（旧副本必须继续服务）==
ok    新副本卡在 ImagePullBackOff
ok    旧副本仍在服务（maxUnavailable=0 生效）

== 6. 演练：回滚 ==
ok    回滚后恢复健康
ok    镜像回到 :local

== 7. 演练：readiness 失败（摘流量但不重启）==
ok    至少一个副本变为 not ready
ok    Endpoint 被摘除
ok    Pod 未被重启（readiness 失败不触发重启）
ok    恢复后重新就绪

== 8. 演练：OOMKilled ==
ok    超过内存 limit 的容器被 OOMKilled
ok    exit code 是 137

== 9. 演练：滚动发布（过程中零 5xx）==
ok    滚动发布期间零 5xx

== 10. 清理可行性 ==
ok    命名空间可删除（不做实际删除，检查无 finalizer 卡住）

========================================
通过 26  失败 0
M8 演练通过。
```

### 3.2 部署状态

```
$ kubectl -n runbookguard get pods -o wide
NAME                             READY   STATUS    RESTARTS   NODE
agent-runtime-...                1/1     Running   0          rg-m8-worker
agent-runtime-...                1/1     Running   0          rg-m8-worker2
console-...                      1/1     Running   0          rg-m8-worker2
control-plane-...                1/1     Running   0          rg-m8-worker2
control-plane-...                1/1     Running   0          rg-m8-worker
mysql-...                        1/1     Running   0          rg-m8-worker
rabbitmq-...                     1/1     Running   0          rg-m8-worker2
redis-...                        1/1     Running   0          rg-m8-worker2
synthetic-lab-...                1/1     Running   0          rg-m8-worker2
synthetic-lab-...                1/1     Running   0          rg-m8-worker

$ curl -o /dev/null -w '%{http_code}' http://127.0.0.1:30080/actuator/health
200
$ curl -o /dev/null -w '%{http_code}' http://127.0.0.1:30081/index.html
200
```

副本分散在两个 worker 上（`topologySpreadConstraints` 生效）。

### 3.3 动作沙箱测试

```
$ cd apps/agent-runtime-python && .venv/bin/python -m pytest tests/test_action_sandbox.py -q
25 passed in 46.94s
```

47 秒是因为它们跑**真实子进程**并起真实 HTTP 服务器。
用 mock 验证进程隔离等于用文档验证文档。

### 3.4 全套测试

```
$ cd apps/agent-runtime-python && .venv/bin/python -m pytest -q
565 passed in 51.14s

$ cd apps/control-plane-java && mvn -o test
[INFO] Tests run: 152, Failures: 0, Errors: 0, Skipped: 0
[INFO] BUILD SUCCESS

$ cd services/synthetic-lab && .venv/bin/python -m pytest -q
35 passed in 1.60s

$ cd apps/console-web && npm test
Tests  23 passed (23)
```

### 3.5 M4 演练在沙箱路径上重跑

动作执行改走子进程后，M4 的审批与恢复演练必须重跑——否则「换成沙箱」
是一次未验证的变更。

```
$ apps/agent-runtime-python/.venv/bin/python scripts/drill-m4.py
...
== 演练 8：审计事件可追溯 ==
ok    审批请求已留痕
ok    审批决策已留痕
ok    审批消费已留痕
ok    存在拒绝类审计事件
ok    篡改参数的尝试被记为 DENIED

===============================
PASS=43  FAIL=0
```

`ActionToolExecutor` 的 `sandbox` 默认为 `True`，演练脚本没有传 `False`，
因此这 43 项是在**子进程路径**上通过的。

---

## 4. Gate 条件自评

DEV_PROMPT §12 M8 的 Gate 条件：**本地 K8s 可部署、可注入故障、可恢复、可回滚、可清理。**

| 条件 | 自评 | 证据 |
|---|---|---|
| 可部署 | **通过** | `scripts/k8s-deploy.sh`，9 Pod Running，NodePort 200 |
| 可注入故障 | **通过** | 错误镜像 / readiness 失败 / OOMKilled / 删 Pod 四类 |
| 可恢复 | **通过** | 删 Pod 后副本自动恢复且服务无中断 |
| 可回滚 | **通过** | `rollout undo` 后镜像回到 `:local` 且重新健康 |
| 可清理 | **通过** | `kind delete cluster` + namespace 无 finalizer 卡住 |

说明书 §20 的具体要求：

| 要求 | 自评 | 备注 |
|---|---|---|
| NetworkPolicy | **通过** | 实测在拦，含 egress 拒公网 |
| requests / limits | **通过** | 演练断言所有容器都有 |
| 三类健康检查分离 | **通过** | 端点、周期、阈值都不同；演练逐条断言 |
| 滚动发布 | **通过** | `maxUnavailable: 0`，过程中零 5xx |
| 故障演练 | **部分通过** | 见下 |
| vLLM 职责边界文档 | **通过** | 无任何性能验证声明 |

DEV_PROMPT 列的七类演练：

| 演练 | 状态 |
|---|---|
| Pod 删除 | **已做** |
| 错误镜像 | **已做** |
| readiness 失败 | **已做** |
| OOMKilled | **已做** |
| CPU throttling | **未做** —— 见 §7.4 |
| MQ backlog | **未做** —— 见 §7.4 |
| Provider timeout | **已做，但在 M3/M6 而非 K8s 上**。`dev-model-timeout` case 与 `test_provider.py` 覆盖了它；K8s 层面没有单独演练，因为它是应用行为不是编排行为 |

---

## 5. 关键设计决定与理由

### 5.1 preStop 的 sleep 不是玄学，是一个实测到的竞态

第一次跑删 Pod 演练时结果是 **33 次探测里有 1 次非 200**。

根因：Pod 被删除时 kubelet 立刻发 SIGTERM，而 Endpoint 的摘除要经过
apiserver → EndpointSlice controller → 每个节点的 kube-proxy 传播。
这两件事**并行**发生，因此在传播完成前仍有新连接被路由到正在关闭的 Pod。

`preStop: sleep 8` 让容器在收到 SIGTERM 后继续服务 8 秒，
覆盖传播延迟（kind 上约 1~2s）。加上之后演练通过。

**这不是测试太严格。** 真实发布同样会掉那 3% 的请求，只是没人在看。
一个只在操作前后各探一次的演练会完全看不到它——这也是为什么演练用后台持续探测
而不是前后对比。

### 5.2 liveness 与 readiness 必须走不同端点

`control-plane` 的 readiness 走 `/actuator/health/readiness`（含数据库连通性），
liveness 走 `/actuator/health/liveness`（**不含**外部依赖）。

用 readiness 组做 liveness 会让一次数据库抖动导致全部副本被重启——
把一个可恢复的依赖故障放大成自身故障。演练里有一条断言专门查这一点。

三个探针的周期也不同：liveness 20s vs readiness 10s。
配同样的阈值会把「启动慢」变成「重启风暴」。
`datasets/runbooks/rb-readiness-probe-failure--v1.1.0.md` 描述的正是这个形态。

### 5.3 演练要断言「不是全拦」

`ok  synthetic-lab 按策略仍可达（证明不是全拦）` 这一条看起来多余，
但它排除了一个具体的假通过：一个全拦的策略与「策略生效」在其它断言上表现一样，
但它会让整套系统不可用。只断言「该拒的拒了」不足以证明策略配对了。

### 5.4 emptyDir 而不是 PVC

这是**演练集群**，每次 apply 从空库开始（Flyway 建表）。
用 PVC 会让「删掉 Pod 看它恢复」变成「删掉 Pod 看它带着旧数据恢复」，
两者要验证的东西不同。

真实部署必须用 StatefulSet + PVC。这一点是缺口而非取舍，记在 §7.1。

### 5.5 动作沙箱默认开启

`ActionToolExecutor(lab_url)` 的 `sandbox` 默认 `True`。
一个「默认不隔离、需要显式打开」的沙箱等于没有沙箱——漏掉一处就是一个缺口。

限制在子进程里施加并**回报**，父进程用 `REQUIRED_LIMITS` 校验。
最初用 `preexec_fn`，但它抛异常时只得到一句不带原因的 `SubprocessError`
（实测：macOS 上 `RLIMIT_AS` 失败时看不出是哪一项）。
必需项缺失即判 `sandbox_failure`：一个没有资源上限的「沙箱」看起来像隔离的，
比明确的同进程执行更危险。

子进程的环境是**白名单**。父进程里有 `RUNBOOKGUARD_LLM_API_KEY`，
继承过去等于把模型凭据交给一个执行外部动作的进程（M0 INV-6）。
用白名单而非黑名单，新增的敏感变量不需要逐个记得排除。

完整的逐条对照见 ADR-0010。

### 5.6 接 vLLM 需要显式加一条 egress 规则

当前 `agent-runtime-policy` 的 egress 白名单里只有 control-plane 与 synthetic-lab，
因此它连不上任何模型服务（演练实测访问 `1.1.1.1:443` 被拒）。

这个「必须显式加」是设计意图：一条新的出站路径应当是一次被审查的动作，
而不是默认就通。

---

## 6. 实际踩到的问题

| # | 现象 | 根因 | 只靠什么才能发现 |
|---|---|---|---|
| 1 | `control-plane` 卡在 `CreateContainerConfigError` | `runAsNonRoot: true` 但只在镜像里写了 `USER runbookguard`（用户名）。kubelet 无法解析用户名是否非 root，直接拒绝创建容器。必须给数字 uid | 真的部署到 K8s。Docker 与 compose 上完全正常 |
| 2 | 删一个 Pod 期间有 1 次非 200 | Endpoint 摘除的传播延迟（§5.1） | **后台持续探测**。前后各探一次看不到 |
| 3 | 演练报「跨命名空间访问 MySQL 被拒」失败，值是 `000000` | curl 连不上时自己会打印 `000` 且退出码非 0，再 `|| echo 000` 拼成了 `000000`。判据从「等于 000」改成「不是 2xx/3xx」 | 看失败输出里的实际值。断言逻辑本身没有语法错误 |
| 4 | 加沙箱后 `test_graph_parity` 一个测试失败：`Exception occurred in preexec_fn` | macOS 上 `setrlimit(RLIMIT_AS)` 抛 `ValueError: current limit exceeds maximum limit`，而 `preexec_fn` 把它变成一句不带原因的错误 | 逐项手动试 setrlimit。错误消息本身指不出是哪一项 |
| 5 | M4 演练 3 项失败：`provider_failure` | `drill-m4.py` 里的 `DIAGNOSIS` 常量是 ADR-0009 **之前**的 schema，改 schema 时没跟上。落 `provider_failure` 是**正确**行为（不许静默修补），但让演练报出假失败 | 重跑演练。单测全绿——它们用的是各自的 fixture |

第 1、4 条同型：**平台/运行时的约束在开发环境里不存在**。
第 5 条是 schema 变更的辐射范围没有被完整跟踪，与 M3 的双 `Diagnosis` 漂移同源，
这次的形态是「脚本里的常量」而不是「模块里的类」。

---

## 7. 已知限制与未验证项

### 7.1 中间件用 emptyDir，不是生产做法

MySQL / RabbitMQ 用 `Deployment + emptyDir`，Pod 重建即丢数据。
真实部署必须 `StatefulSet + PVC + PodDisruptionBudget`。
演练里「删 Pod 后恢复」验证的是**应用层**恢复，不是数据持久性。

### 7.2 动作沙箱的两项缺口需要容器才能补

| 要求 | 状态 |
|---|---|
| 精确挂载 | **未实现**。子进程与父进程共享文件系统视图 |
| 只读根文件系统 | **未实现**。`RLIMIT_FSIZE=0` 禁止写文件，但 `/` 仍可读 |

补它们需要把动作放进独立容器（K8s Job / sidecar / 外部 runner 服务），
那是一次架构变更。ADR-0010 §未采用的方案里说明了为什么不用
Docker-in-Docker：为了限制动作而给执行者 Docker socket 或 K8s 写权限，方向是反的。

另外 `sandbox=False` 这条路径的存在本身是风险。有测试断言默认值是 `True`，
但没有机制阻止有人在生产代码里传 `False`。

### 7.3 K8s 里没有观测栈

集群里不部署 Prometheus / Grafana / collector——那是 compose 的职责。
因此 ConfigMap 里 `RUNBOOKGUARD_OTEL_ENABLED=false`。
**K8s 环境下的指标与 trace 未验证。**

### 7.4 两类演练未做

| 演练 | 为什么没做 |
|---|---|
| CPU throttling | 需要构造持续 CPU 压力并观察 `container_cpu_cfs_throttled_seconds_total`，而集群里没有 Prometheus（§7.3），只能靠 `kubectl top`，分辨率不足以支撑一个可信断言。**不做胜过做一个测不准的** |
| MQ backlog | 需要往 RabbitMQ 灌消息并观察队列深度。灌消息需要一个生产者，而 Worker 的消息生产逻辑在 Java 侧且需要真实 Run 驱动。M2 的 `drill-m2.sh` 在 compose 上覆盖了重投与恢复；K8s 层面的 backlog 演练**未做** |

### 7.5 CI 的 k8s job 未在 GitHub 上实跑（UNKNOWN）—— 已关账（2026-09-13 后记）

> 后记：k8s job 第四跑在 runner 上全绿——kind 三节点集群、CNI 执行 NetworkPolicy
> 的核验、部署、26 项演练全部通过。过程中修了两处本报告没有预见的 runner 差异：
> GNU mktemp 模板差异导致演练死循环（§6「macOS 能跑 Linux 不能跑」的第二例），
> 以及 1Gi 内存 limit 对 JVM 启动峰值是擦边值（两副本同窗口重启）。本节其余内容
> 按当时事实保留。

与 M7 §7.1 同一情况：`k8s` job 的每条命令都在本地验证过，
但 workflow 本身未被 GitHub Actions 执行过。

具体风险：
- `helm/kind-action@v1` 在 runner 上建三节点集群的资源占用（runner 是 2 核 7GB）
- `kind load docker-image` 四个镜像的耗时
- 45 分钟 timeout 是否够

推送后第一次运行大概率需要调整。不该被写成「CI 已通过」。

### 7.6 无真实 GPU

`M8-model-gateway-and-vllm-boundary.md` 里**没有任何性能数字**。
吞吐、首 token 延迟、并发上限、KV cache 命中率全部未测。
文档里出现的机制描述（PagedAttention、continuous batching）来源是公开资料，
已逐条标注。

该文档 §6 列了 5 个缺口，其中第 1 条最需要先解决：
**自托管后端没有 per-token 计价，`pricing.py` 的 `lookup()` 返回 `None`
会让成本记为 0，从而使 cost budget 这条终止条件静默失效。**

### 7.7 NodePort 的 NetworkPolicy 表达不精确

NodePort 流量的源地址是节点 IP 而非 Pod IP，无法用 `podSelector` 表达，
因此 `control-plane-policy` 与 `synthetic-lab-policy` 的 ingress 含 `0.0.0.0/0`。
代价是控制台以外的宿主机进程也能访问这些端口。

生产环境应当用 Ingress + 认证代理替代 NodePort，那时可以收紧到
ingress controller 的 Pod 标签。

### 7.8 延续的旧缺口

| 项 | 状态 |
|---|---|
| 提交代码后重跑冻结评测（M6 §10 A1） | **已做**。第 4 轮 commit `ca42a6a`、`working_tree_clean=true`、10 项达标 + Replay 一致 |
| checkpoint 元数据落 MySQL | **未做**。仍在内存 |
| LangGraph checkpointer | 仍是 `InMemorySaver` |
| 检索 latency / cost 按阶段拆分 | **未做** |
| Trace 与 OTel span 的关联 | **未做**。两套 id，控制台无法从 Trace 跳到 span |
| actuator 端口未分离（M7 §7.5） | **未做**。`/actuator/prometheus` 无鉴权 |

---

## 8. 可复现命令

```bash
# 前置（一次性）
brew install kind          # kubectl 已在

# 建集群
kind create cluster --config deploy/k8s/kind-cluster.yaml --image kindest/node:v1.34.0

# 先核验 CNI 是否真的执行策略。不执行时后面所有「被拒绝」的断言都是假通过
bash scripts/verify-networkpolicy-enforcement.sh kind-rg-m8

# 构建 + 装载 + 部署 + 等就绪
bash scripts/k8s-deploy.sh kind-rg-m8

# 26 项故障演练
bash scripts/drill-m8.sh kind-rg-m8

# 动作沙箱测试（跑真实子进程，约 47s）
cd apps/agent-runtime-python && .venv/bin/python -m pytest tests/test_action_sandbox.py -q

# 清理
kind delete cluster --name rg-m8
```

---

## 9. 全项目状态

M0 → M8 全部里程碑已交付。九份 Gate 报告与十份 ADR 在 `docs/`。

**允许写进简历的依据**：M6 Gate 通过（冻结评测 10 项阈值全达标）。
写的时候必须连带三条限制，否则会被一个问题问穿：

1. **冻结评测用的是脚本化 provider**，10 项 1.0000 是机制正确性，不是诊断准确率。
   真实模型（glm-5.3-flash）在 12 个诊断 case 上是成功率 0.6667、归因 0.8000。
2. **`incidents-held` 与 dev 同一作者设计**，共用故障剧本与判据代码。
   held 上的成绩只证明产品代码里没有针对 dev 的硬编码，不构成泛化能力证明。
3. **CI 未在 GitHub 上实跑**（M7 §7.1、M8 §7.5）；**无真实 GPU**，
   vLLM 部分只有架构理解与客户端契约。

最需要优先补的两件事，按代价从低到高：
- `pricing.py` 对自托管后端的计价（§7.6 第 1 条）——它会让一个安全机制静默失效
- 动作工具的容器隔离（§7.2）——补完 M0 §9 的最后两项（精确挂载、只读根文件系统）

M6 §10 A1（提交后重跑冻结评测）已在第 4 轮完成：commit `ca42a6a`、
`working_tree_clean=true`、10 项阈值达标 + Replay 一致。
那一轮同时修了一个更重要的东西——冻结清单漏了「观测窗口」，
导致第 1、2 轮的 Replay「一致」其实是运气（M6 报告 §5.2）。
