# 整改审计报告（M8 收尾）

> 性质：M8 之后的一轮系统审计与整改（用户指令：审计整改、按正规项目计划开发直至完成）
> 日期：2026-09-12
> 审计方式：全量检索 10 份 Gate 报告的 UNKNOWN / 未验证 / 未做 标记（130 处，去重后如下）、
> 全仓库 TODO/FIXME 扫描（**0 处**）、脚本与当前 schema 一致性抽查、
> git 与远端状态核对、报告声明与代码现状比对

---

## 1. 审计发现的完整清单与处置结果

### A 类：发布前必须（3 项）

| # | 发现 | 处置 | 结果 |
|---|---|---|---|
| A1 | **无 LICENSE**。公开仓库必需 | 新增 `LICENSE`（MIT，版权人 LJunP） | ✅ 完成 |
| A2 | **远端已配置**（github.com/LJunP/RunbookGuard-Agent-Platform），本地领先 3 个 commit；CI 从未在 GitHub Actions 上跑过 | **已完成**（2026-09-13，所有者授权推送）。CI 共 4 次运行：首跑 5/6 绿（k8s job 因 GNU mktemp 差异死循环超时）、二跑暴露测试顺序依赖与抓取竞态、三跑 k8s 抖动（1Gi limit 擦边 OOM）、四跑 **6/6 全绿**（run 34741559390）。修了 4 处 runner 差异，每次一个 commit | ✅ 完成 |
| A3 | 所有者验收审查未做 | 10 份 Gate 报告在 `docs/architecture/` | ⏳ 待所有者执行 |

### B 类：功能缺口（5 项，本轮完成 4 项）

| # | 发现 | 处置 | 结果 |
|---|---|---|---|
| B1 | **pricing 未知模型 fail-closed 缺失**（M8 报告 §6 头号缺口）：计价表查不到时 cost_micros 恒为 0，配了 MAX_COST_MICROS 时 `cost_budget_exceeded` 终止条件看起来在工作、实际永不触发——一个静默失效的安全机制比没有更糟。审计证实 `from_env` 确实支持 `RUNBOOKGUARD_LLM_MAX_COST_MICROS`，缺口是真实可达的 | `ProviderConfig.__post_init__` 增加 fail-closed：预算>0 且模型无计价 → 拒绝构造，提示把模型加进 `pricing.py` 或显式设 `RUNBOOKGUARD_ALLOW_UNKNOWN_PRICING=1`（显式承认预算不可执行的 escape hatch，默认关闭）。`eval-m6-real-model.py` 的 warn-and-continue 改为拒绝 | ✅ 完成，6 个新测试 |
| B2 | **checkpoint 无持久化**：LangGraph checkpointer 是 `InMemorySaver`（M8 集群里也显式关闭），进程一死恢复能力归零；checkpoint 元数据不落 MySQL（M6 §10 A4） | ① 新增 `agent/checkpoint_persistence.py`：`RUNBOOKGUARD_CHECKPOINT_DB` 指向 sqlite 文件时状态跨进程存活（含「写→关连接→新连接读回」的直接验证）；② `RecordingCheckpointer` 在每次写入时把元数据（id / 序号 / 状态摘要）fire-and-forget 上报控制面，失败可见（日志 + `runbookguard_checkpoint_metadata_failures_total`）但不中断 Run；③ Java 侧新增 `CheckpointController`（POST/GET `/api/v1/runs/{id}/checkpoints`），`INSERT IGNORE` 幂等，submitted 与 written 分开返回 | ✅ 完成，13 个 Python 测试 + 2 个 Java 集成测试 |
| B3 | **Trace 与 OTel span 两套 id 无法互跳**（M6 §10 A6 的「关系」部分） | `RunTrace` 增加 `otel_trace_id`（自动取当前活跃 span），写盘与读回支持；**不进 digest**——trace_id 每次运行必然不同，与 run_id 同理。只存 id 不存 span：span 归观测栈，Trace 归审计 | ✅ 完成，3 个新测试 |
| B4 | **检索延迟只有总延迟**（说明书 §16 指标缺拆分） | `HybridRetriever` 对每个子检索器按阶段计时（`runbookguard_retrieval_stage_duration_seconds{stage=...}`），出现问题时能区分是 BM25 慢还是向量查询慢 | ✅ 完成 |
| B5 | 动作工具的容器级隔离（精确挂载、只读根文件系统） | **未整改**。进程级沙箱已覆盖 M0 §9 的其余条目（ADR-0010）；容器级需要独立的 action-runner 服务，是一次架构变更。保留为已知缺口 | ⏳ 已知缺口 |

### C 类：验证空白（4 项）

| # | 发现 | 处置 |
|---|---|---|
| C1 | CI 从未实跑 | **已关账**：见 A2。runner 上 6/6 全绿 | ✅ 完成 |
| C2 | K8s 环境下指标与 trace 未验证（集群无观测栈） | 保持现状。要验证需在集群里部署 Prometheus/collector——M8 已论证观测栈归 compose 职责；如需验证是新增工作而非整改 |
| C3 | CPU throttling / MQ backlog 两类 K8s 演练未做 | 保持 M8 报告的理由：前者无集群内 Prometheus，分辨率不足以支撑可信断言；后者需要 Java 侧生产者。不做胜过做一个测不准的 |
| C4 | held 无 Replay | 刻意设计（held 只跑一次的纪律）。M6 报告 §7.4 已写明。**不整改**——给 held 加 Replay 等于让 held 跑两次，破坏它的独立性 |

### D 类：审计新发现的陈旧缺陷（3 项，全部完成）

| # | 发现 | 处置 | 结果 |
|---|---|---|---|
| D1 | **`scripts/dev-baseline.py` 仍在用 ADR-0009 之前的 Diagnosis schema**——现在跑会全落 `provider_failure`。且它自带的 `provider_for` 会**覆盖** case 声明的 `model_script`，与 45 case 套件不兼容（实测 0.3556 成功率，全是脚本问题） | 改用 harness 默认工厂（与冻结评测同一套 `provider_for_case`），删除自带 provider 与审批替身；头部标注已被 `eval-m6-frozen.py` 取代 | ✅ 实测 1.0000 / 1.0000 / 1.0000 |
| D2 | `.gitignore` 白名单了 `.env.example` 但该文件不存在 | 新增完整模板（LLM 凭据 + compose 端口 + 中间件凭据），含「未知模型 + 成本预算会被拒绝」的提示 | ✅ 完成 |
| D3 | **业务口对未知路径返回 500 而非 404**（整改 S3 时撞出）：`@ExceptionHandler(Exception.class)` 把 Spring 6.1+ 的 `NoResourceFoundException` 也吞成 internal_error。任何打错路径的请求都变成 5xx，监控告警会被噪音淹没，与控制面真坏无法区分 | `ApiExceptionHandler` 增加对 `NoResourceFoundException` 的处理（404 not_found）；旧测试 `healthEndpointIsOpen` 改为断言端口分离生效 | ✅ 完成，实测 `{"error":"not_found"}` |

### E 类：顺手根治的构建问题（1 项）

| 发现 | 处置 |
|---|---|
| Dockerfile 安装 wget 仅服务于 compose 健康检查；本次整改时 Docker Hub 元数据拉取持续超时，导致镜像无法重建 | `control-plane` 健康检查改走 bash `/dev/tcp`（qdrant 先例），**从 Dockerfile 删除 apt-get/wget**。构建期除基础镜像与 Maven 依赖外不再需要网络。镜像出不来时用本地 jar + `docker commit` 组装的临时通道也已验证可行（仓库 Dockerfile 仍是 CI 正路） |

---

## 2. 整改后的回归验证

```
Python Agent Runtime   592 passed（新增 22：pricing 6 + checkpoint 13 + trace_id 3）
Java Control Plane     155 passed（新增 3：404 路径 1 + checkpoint 2）
synthetic-lab           43 passed
console-web             23 passed
M7 冒烟                 30 项全过（新增「业务端口不再暴露 actuator」）
M4 演练                 43 项全过（健康检查改走管理端口）
```

compose 实测（actuator 端口分离 + 404 修复）：

```
业务口 :8080/actuator/health     → 404（整改前 200，管理信息外泄）
管理口 :9080/actuator/health     → 200
管理口 :9080/actuator/prometheus → 200
业务口 /api/v1/incidents         → 200
未知路径 /no-such-path           → 404 {"error":"not_found"}（整改前 500）
```

k8s manifest 同步：探针全部改走 9080（startup/readiness/liveness），`containerPort` 命名
business/management，`control-plane-policy` 为 kubelet 探针放行 9080（无 NodePort，不对外）。

---

## 3. 当前项目状态的诚实结论

**开发与整改层面没有未完成项**——B5（容器隔离）与 C 类验证空白是**已知且书面记录的边界**，
不是被遗忘的工作。

距离「公开发布」还差两步，都不在开发范畴内：
1. ~~`git push`——触发 CI 首跑~~ **已完成**：仓库已同步，CI 6/6 全绿（run 34741559390）
2. 所有者验收审查

**CI 达成的过程本身值得记录**：4 次运行、3 次修复，每一次失败都是本地永远不会出现的差异——GNU mktemp 模板（BSD 收、GNU 拒，导致演练死循环烧掉 39 分钟）、测试类执行顺序（append-only 触发器把顺序依赖变成失败）、Prometheus 抓取竞态、1Gi limit 对 JVM 的擦边 OOM。「本地全绿 ≠ runner 全绿」从一句口号变成了四次实证。
