# RunbookGuard Agent Platform

面向 SRE / DevOps / 中小研发团队的**故障诊断与受控处置 Agent 平台**。它读取告警、指标、日志、部署记录和版本化 Runbook，调用受限工具收集证据，生成带引用的故障判断；在执行任何动作之前必须通过服务端权限校验与人工审批；在 Worker 或 Agent 进程中断后能从 Checkpoint 恢复到唯一终态。

它不是聊天机器人，不是可以任意执行 shell 的"全自动运维 Agent"，也不是又一个 RAG 问答。

**诊断能力不是这个项目的护城河。** HolmesGPT 与 kagent 已经在做 SRE Agent。这里的内核是：**如何让一个会调用工具的 AI 在运维场景里不出事，并且能被证明不出事。**

## 当前状态：M0 → M8 全部交付

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 问题与边界冻结 | 已交付 |
| M1 | Java Control Plane | 已交付 |
| M2 | 异步 Worker（Lease / 幂等 / 恢复） | 已交付 |
| M2.5 | Synthetic Lab（9 个确定性故障剧本） | 已交付 |
| M3 | 模型 Runtime（Provider Adapter / 结构化输出） | 已交付 |
| M4 | Agent / Tool / MCP / HITL | 已交付 |
| M5 | RAG 与引用（36 个版本化 Runbook） | 已交付 |
| M6 | Evaluation 与安全（冻结评测通过） | 已交付 |
| M7 | React 控制台 / Compose 一键起 / CI / OTel / Grafana | 已交付 |
| **M8** | **kind 集群 / NetworkPolicy / 故障演练 / 动作沙箱** | **已交付** |

### M6 冻结评测结果（第 4 轮，`incidents-held`，2026-08-30）

| 指标 | 阈值 | 实测 |
|---|---|---|
| 安全红线拒绝率 | 100% | 1.0000 |
| Tool Schema 合法率 | 100% | 1.0000 |
| citation validity | ≥ 95% | 1.0000 |
| 固定任务成功率 | ≥ 80% | 1.0000 |
| 中断恢复唯一终态率 | 100% | 1.0000 |
| answer groundedness | 100% | 1.0000 |
| 归因正确率 | 100% | 1.0000 |
| 弃答正确率 | 100% | 1.0000 |
| 违规检出率 | 100% | 1.0000 |
| Replay 行为一致性 | 逐字节 | 45/45 一致 |

**这些数字必须连同两条限制一起读**，否则会被误解：

1. 冻结评测用的是**脚本化 provider**，量的是编排与安全机制的正确性，不是诊断准确率。
2. `incidents-held` 与 `incidents-dev` 由同一作者设计，共用同一批故障剧本与判据代码。held 上的成绩只证明「产品代码里没有针对 dev 具体 case 的硬编码」，不构成泛化能力的证明。

3. CI 的 workflow 未在 GitHub 上实跑过（本地逐条验证了它跑的命令）；没有真实 GPU，vLLM 部分只有架构理解与客户端契约。

真实模型（glm-5.3-flash）在 12 个诊断 case 上的实测：**成功率 0.6667、归因正确率 0.8000**。完整数据与逐条失败分析见 [M6 Gate 报告](docs/architecture/M6-gate-report.md) §7.2b。

冻结评测跑了四轮，四份报告全部保留（`eval/reports/`）。四轮的原因都不是「数字不好看想重跑」：第 1 轮暴露了评测工具自身的分母定义错误，第 3 轮暴露了**前两轮的 Replay「一致」是运气**——观测窗口没被冻结，而两轮各跑 17 秒恰好落在同一分钟内。一个通过原因不成立的检查比失败的检查更危险。逐轮经过与「为什么这不属于为了数字改口径」的逐项对照见 [M6 Gate 报告](docs/architecture/M6-gate-report.md) §5。

## 一键起（M7 Gate 条件）

前置：Docker Desktop 或等价的 Docker 环境。**不需要**配置任何模型凭据——默认走 fake provider，零网络零成本。

```bash
git clone <this-repo> && cd RunbookGuard-Agent-Platform

# 1. 起全栈（首次构建约 5~10 分钟）
docker compose -f deploy/compose/docker-compose.yml up -d --build

# 2. 等就绪
bash scripts/wait-for-stack.sh

# 3. 冒烟（29 项检查：CORS / RBAC / 审批链路 / 观测接线 / trace 导出）
bash scripts/smoke-m7.sh
```

起来之后：

| 界面 | 地址 | 说明 |
|---|---|---|
| 控制台 | [http://127.0.0.1:8081](http://127.0.0.1:8081) | Incident / Run / Trace / 审批 / 审计 |
| Grafana | [http://127.0.0.1:3000](http://127.0.0.1:3000) | RunbookGuard 概览面板（匿名只读，本地专用） |
| Prometheus | [http://127.0.0.1:9090](http://127.0.0.1:9090) | 抓取目标状态 |
| Control Plane | [http://127.0.0.1:8080](http://127.0.0.1:8080) | Java 控制面 API |
| Agent Runtime | [http://127.0.0.1:8100](http://127.0.0.1:8100) | Python 运行时 |
| Synthetic Lab | [http://127.0.0.1:8090](http://127.0.0.1:8090) | 故障数据源 |
| RabbitMQ 管理台 | [http://127.0.0.1:15673](http://127.0.0.1:15673) | `runbookguard` / `local-dev-only` |

清理：`docker compose -f deploy/compose/docker-compose.yml down -v`

### 本地演示凭据

`RUNBOOKGUARD_SEED_DEV_DATA=true`（compose 里已开）会写入四个固定 token。它们是**本地专用**，任何真实部署都不该打开这个开关：

| token | 角色 | 能做什么 |
|---|---|---|
| `dev-viewer-token` | VIEWER | 只读：Incident / Run / Trace / 审计 |
| `dev-operator-token` | OPERATOR | 创建 Incident 与 Run |
| `dev-approver-token` | APPROVER | 批准或驳回审批 |
| `dev-agent-token` | AGENT_RUNTIME | 发起审批、上报 Trace。**不含 APPROVER** |

控制台顶部粘贴 token。它只存在内存里，不写 localStorage。

### 演示流程

1. 用 `dev-operator-token` 或 `scripts/smoke-m7.sh` 造一个 Incident 与 Run。
2. 控制台「Incident 与 Run」页选中 Run，看时间线、预算、证据与引用、审批链路。
3. 用 `dev-approver-token` 切到「待审批」，看到参数原文与摘要，批准或驳回。
   - 用 `dev-agent-token` 试着批准同一条 → 403。Python 侧不存在能放行的代码路径。
4. 「审计事件」页看 DENIED 记录。被拒绝的尝试比允许的更重要，它们不会因回滚而消失。

## 本地 K8s（M8）

Compose 是演示环境；K8s 是验证编排行为的环境（NetworkPolicy、探针分离、故障演练）。

```bash
brew install kind    # kubectl 已在则不需要额外装

kind create cluster --config deploy/k8s/kind-cluster.yaml --image kindest/node:v1.34.0

# 先核验 CNI 是否真的执行 NetworkPolicy。
# 不执行时后面所有「被拒绝」的断言都会变成假通过。
bash scripts/verify-networkpolicy-enforcement.sh kind-rg-m8

bash scripts/k8s-deploy.sh kind-rg-m8    # 构建 + kind load + apply + 等就绪
bash scripts/drill-m8.sh kind-rg-m8      # 26 项故障演练

kind delete cluster --name rg-m8
```

演练覆盖：三类探针确实分离、NetworkPolicy 实测在拦（含 agent-runtime 访问公网被拒）、
删 Pod 与滚动发布期间零失败请求、错误镜像不换掉好副本、readiness 失败摘流量但不重启、
OOMKilled 拿到 exit code 137、回滚可用。

已核验：**kindnetd v20250512 会执行 NetworkPolicy**（deny-all 后连接从 200 变 000），
因此不需要换 Calico。kindnetd 的 README 既没说支持也没说不支持，这一条只能实测。

集群里 `RUNBOOKGUARD_OTEL_ENABLED=false`：观测栈是 compose 的职责，
因此 K8s 环境下的指标与 trace 未验证。

## 用真实模型跑（可选）

默认 fake。要接真实 Provider：

```bash
export RUNBOOKGUARD_LLM_PROVIDER=openai-compatible
export RUNBOOKGUARD_LLM_BASE_URL=https://your-gateway/v1
export RUNBOOKGUARD_LLM_MODEL=your-model
export RUNBOOKGUARD_LLM_API_KEY=sk-...
docker compose -f deploy/compose/docker-compose.yml up -d agent-runtime
```

凭据只从环境变量注入，绝不写入任何仓库文件。`.gitignore` 屏蔽 `.env*` / `*.key` / `*.pem`；所有落盘产物过 `redact()`。

## 架构

```
                React Console (TS + React)          :8081
                        |  诊断展示 / Trace 查看 / 审批操作
                        v
        Java Spring Boot Control Plane              :8080
          Identity / Tenant / RBAC
          Incident / Run / Approval / Trace
          Policy / Audit / Artifact
                        |
            +-----------+-----------+
            v           v           v
         MySQL 8      Redis      RabbitMQ
                                    |
                                    v
        Python FastAPI Agent Runtime               :8100
          Provider Adapter / LangGraph
          Checkpoint / Retrieval / Evaluation
                        |
            +-----------+-----------+
            v                       v
         Qdrant :6333       MCP Tool Gateway
                                    |
                    +---------------+---------------+
                    v               v               v
              只读工具 x5      审批型动作工具 x3   synthetic-lab :8090

全链路 --> OTel Collector :4318 --> Prometheus :9090 --> Grafana :3000
```

动作工具（`restart` / `rollback` / `throttle`）在**受限子进程**里执行：
非 root、CPU/内存/PID 上限、禁止写文件、环境变量白名单（模型凭据不继承）、
超时强制 SIGKILL、egress 白名单。挂载隔离与只读根文件系统仍是缺口——
需要容器才能做到，见 [ADR-0010](docs/adr/ADR-0010-action-sandbox-and-k8s-boundary.md)。

**职责切分**：Java 拥有一切业务事实（用户、租户、权限、Incident、Run、Approval、Trace 的权威状态、事务、幂等、审计）。Python 拥有一切智能与检索（Provider、LangGraph 编排、Tool Calling、RAG、Evaluation），它不是业务真相的持有者。

**Approval 的权威判定在 Java 侧。** Python 只能 `request` 与 `consume`，代码里不存在 `approve` 这个方法。

## 核心设计：三段分离

```
模型输出 ToolCall 建议  →  Policy 校验（服务端，可拒绝）  →  Executor 执行
   suggestion              authorization                    execution
```

三段不在同一个函数里。`ToolAuthorization(allowed=True)` 只能由 `issue_authorization()` 构造，后者受 `_POLICY_ISSUER_TOKEN` 保护——类型系统层面禁止绕过 Policy 直接执行。

写动作还需人工审批，审批绑定参数摘要（`arguments_digest`，`JCS-SHA256-V1`）。执行前重新比对四项（审批 id / 工具名 / 资源 / 参数摘要），任一不符即拒绝。

日志、指标、Tool 结果、Runbook 全部划为**不可信数据**，不能改变控制流。日志里出现 "ignore previous instructions" 是数据，不是指令——评测集里有 7 个 case 专门检验这一点。

## 各语言测试

```bash
# Java：152 个测试，Testcontainers 起真实 MySQL/Redis/RabbitMQ，不用 mock
cd apps/control-plane-java && mvn test

# Python Agent Runtime：565 个测试（含 25 个跑真实子进程的沙箱测试，约 50s）
cd apps/agent-runtime-python && pip install -e ".[dev]" && python -m pytest -q

# Synthetic Lab：35 个测试
cd services/synthetic-lab && pip install -e ".[dev]" && python -m pytest -q

# 控制台：23 个测试 + 类型检查
cd apps/console-web && npm ci && npm run typecheck && npm test
```

CI 把这些拆成 6 条独立 job（Java / Python / Console / 跨语言契约 / Compose 一键起 / kind 集群演练）。拆开的理由：一个语言的失败不该掩盖另一个语言的状态。

**CI 的 workflow 未在 GitHub 上实跑过** —— 每条命令都在本地验证了，但 runner 环境的差异（Docker CE vs Desktop、资源限制、缓存键）大概率需要调整。这一点不该被读成「CI 已通过」。

## 演练与评测脚本

| 脚本 | 用途 |
|---|---|
| `scripts/smoke-m7.sh` | M7 冒烟：29 项接线检查 |
| `scripts/wait-for-stack.sh` | 等全栈就绪 |
| `scripts/drill-m2.sh` | Worker kill -9 与消息重投演练 |
| `scripts/drill-m4.py` | 审批、篡改参数、跨进程恢复演练（43 项） |
| `scripts/verify-m2_5-determinism.sh` | 同一剧本连跑 3 次产出逐字节一致 |
| `scripts/eval-retrieval.py` | Recall@K / MRR@K / citation validity |
| `scripts/eval-m6-frozen.py` | 正式冻结评测（拒绝覆盖已有报告） |
| `scripts/eval-m6-real-model.py` | 真实模型诊断质量测量（不参与 Gate 判定） |
| `scripts/generate-incidents-held.py` | 生成 held 数据集（只打印统计，不打印内容） |
| `scripts/verify-networkpolicy-enforcement.sh` | 核验 CNI 是否真的执行 NetworkPolicy |
| `scripts/k8s-deploy.sh` | kind 集群构建 + 装载 + 部署 |
| `scripts/drill-m8.sh` | K8s 故障演练（26 项） |

## 文档

| 文档 | 内容 |
|---|---|
| [M0 Product Brief](docs/architecture/M0-product-brief.md) | 项目定义、差异化、成功标准 |
| [M0 用户与场景](docs/architecture/M0-users-and-scenarios.md) | 5 个画像、5 条旅程 |
| [M0 不做清单](docs/architecture/M0-non-goals.md) | 17 条硬性约束，每条含理由与重新评估条件 |
| [M0 威胁模型](docs/architecture/M0-threat-model.md) | 攻击者模型、6 条不变式、12 条威胁 |
| [M0 初始故障场景](docs/architecture/M0-initial-incident-scenarios.md) | 8 个场景 × 7 项定义 |
| [M1](docs/architecture/M1-gate-report.md) … [M8](docs/architecture/M8-gate-report.md) Gate 报告 | 每份含真实运行输出、Gate 逐条自评、**实际踩到的问题**、已知限制 |
| [vLLM 与模型网关边界](docs/architecture/M8-model-gateway-and-vllm-boundary.md) | 三层职责、客户端契约。**无任何性能验证**（没有 GPU） |
| [ADR-0001](docs/adr/ADR-0001-scope-freeze-and-spec-conflicts.md) … [ADR-0010](docs/adr/ADR-0010-action-sandbox-and-k8s-boundary.md) | 10 份决策记录，含当日核验结果 |
| [开发主提示词](docs/DEV_PROMPT.md) | 开发契约（五条铁律、里程碑与 Gate 定义） |

Gate 报告里的「实际踩到的问题」一节值得优先读：它记录了单测全绿而容器崩溃、判据存在但结构上永不触发、防御条件写反方向这几类缺陷，以及各自只有什么手段能发现。

## 明确不做（摘要）

任意 shell 执行；真实生产 Kubernetes 写入；删除类工具；任意公网访问；Secret 进入 Prompt/Trace/日志/Artifact/报告；无上限 Agent 循环；复杂多 Agent 编排；自研模型或训练框架；Kafka / Elasticsearch / Milvus / Temporal / 第二个 Agent 框架。

完整清单与逐条理由见[不做清单](docs/architecture/M0-non-goals.md)。

所有处置动作只作用于 `services/synthetic-lab/`，且只允许三个动作工具：`restart_synthetic_service`、`rollback_synthetic_deployment`、`throttle_synthetic_traffic`。

## 同类项目

本项目不宣称发明这个领域。已核验的同类工作（核验日期 2026-08-26）：

- [HolmesGPT](https://github.com/robusta-dev/holmesgpt) — CNCF Sandbox 的 SRE Agent，面向生产事件调查与根因分析。
- [kagent](https://github.com/kagent-dev/kagent) — CNCF 的云原生 Agent 框架，Agent / ModelConfig / ToolServer 为 Kubernetes CRD。

差异化不在诊断能力，而在于**如何让一个会调用工具的 AI 在运维场景里不出事，并且能被证明不出事**：权限与审批边界、可评测性、崩溃可恢复。
