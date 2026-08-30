# ADR-0004：Synthetic Lab 的故障剧本格式与实现方式

- Status: Accepted
- Date: 2026-08-26
- 里程碑：M2.5（M2 Gate 报告 §7.1 的待决事项 B2 / B3）
- 关联：[ADR-0001 C-1 / C-5](ADR-0001-scope-freeze-and-spec-conflicts.md)、[M0 初始故障场景](../architecture/M0-initial-incident-scenarios.md)

## Context

M2.5 Gate 的硬条件是：**同一个故障剧本连续跑 3 次，产出的指标与日志特征稳定可复现**。这一条决定了几乎所有实现选择——它排除了任何依赖真实时间、真实负载、真实并发调度的做法。

三个必须先定的问题：

1. 剧本用什么形式表达（声明式数据 vs 代码）？
2. synthetic-lab 用什么语言实现？
3. "稳定可复现"到什么程度算达标？

背景约束：M0 场景 S1-S4 需要 5 类可查询数据（ADR-0001 C-5 已裁定从 4 类增加容器/运行时事件）；S6 需要能把任意文本（含注入载荷）植入日志；S7/S8 需要"没有对应 Runbook"和"证据互相矛盾"这类刻意构造的输入。

## Decision

### 1. 剧本是声明式 YAML，不是代码

一个剧本 = 一份声明「在相对时间轴上，哪些服务的哪些信号呈现什么形态」的数据文件。

```yaml
id: db-pool-exhaustion-v1
version: 1
description: 发布引入连接泄漏，连接池耗尽导致请求排队超时
seed: 20260826            # 决定所有伪随机抖动
timeline:
  baseline_minutes: 30    # T0 之前
  incident_minutes: 5     # T0 之后
services:
  - name: synthetic-orders
    metrics:
      db_pool_active: {baseline: 12, incident: 50, shape: ramp}
      db_pool_max:    {baseline: 50, incident: 50, shape: flat}
```

**为什么不用代码**：剧本必须能被评测集引用、版本化、diff、以及在 Gate 报告里逐字贴出来。代码形式的剧本无法回答"这次评测用的剧本和上次差在哪"——而 M6 要求冻结 dataset，这是硬需求。

代价：表达能力受限于预定义的 `shape` 词表（flat / ramp / step / spike / sawtooth）。这是刻意的：无限的表达能力会让"稳定可复现"无法保证。需要新形态时扩词表，而不是让剧本里写逻辑。

### 2. 时间轴是相对的，数据在查询时按锚点生成

剧本不描述绝对时间，只描述相对于 `T0`（故障注入时刻）的偏移。查询接口在被调用时，按「剧本启动时记录的 T0」把相对时间轴映射到绝对时间戳。

**关键推论：数据不是"随时间推移逐渐产生"的，而是按查询窗口即时计算出来的。** 剧本启动后立刻查询 T0+5min 的数据，会得到完整的故障期数据，不需要真的等 5 分钟。

**为什么**：如果数据靠后台线程随真实时间累积，那么"连续跑 3 次"必然产生差异——线程调度、GC、系统负载都会渗进数据里。而且评测 30~50 个 case 每个都等 5 分钟是不可接受的。

代价：这不是一个"真的会慢下来的服务"，而是一个"能回答任意时间窗口查询的故障数据源"。M2.5 的定位就是后者（M0 §7 称其为"只读工具的数据来源"）。真实的资源压力演练在 M8 用 Kubernetes 做。

### 3. 所有随机性由剧本的 seed 决定

指标的抖动、日志的行间隔、错误消息的轮换，全部来自 `seed` 派生的确定性伪随机序列。同一 seed + 同一查询窗口 → 逐字节相同的输出。

具体做法：每个 `(scenario_id, service, signal, bucket_index)` 组合派生一个独立的子种子，用 `hashlib.blake2b` 而不是 `random.Random` 的全局状态——后者的输出取决于调用顺序，一旦并发查询或新增信号就会全盘变化。

**"稳定"的判定标准**：连续 3 次运行同一剧本、查询同一窗口，返回体的 SHA-256 完全相同。这比"关键判据稳定"更严格，但更容易验证，也排除了"看起来差不多"的模糊空间。唯一允许变动的是绝对时间戳（因为 T0 不同）——因此稳定性校验对时间戳做归一化后再比对。

### 4. 实现语言：Python 3.12 + FastAPI

**理由**：与 M3 的 Agent Runtime 同栈，只维护一套工具链与一套依赖管理。Java 侧已经承载了业务事实，没必要让它再兼一个数据模拟器。

不选 Java 的额外原因：synthetic-lab 需要频繁调整信号形态（M6 扩充到 13 类故障时），Python 的迭代成本更低，而这部分代码不承担任何安全或一致性职责——它是测试替身，不是产品路径。

### 5. 五类查询接口

| 接口 | 对应只读工具 | 关键字段 |
|---|---|---|
| `GET /v1/metrics` | `get_service_metrics` | service、metric、时间序列点（timestamp / value） |
| `GET /v1/logs` | `search_service_logs` | timestamp、level、service、message、可选 attributes |
| `GET /v1/deployments` | `get_recent_deployments` | service、version、deployed_at、changed_config_keys（**只有键名，无值**） |
| `GET /v1/queues` | `get_queue_state` | queue、depth、oldest_age_seconds、consumer_count、publish_rate、deliver_rate |
| `GET /v1/runtime-events` | 待定（ADR-0001 C-5） | service、event_type、timestamp、restart_count、exit_code、reason |

第五类是 ADR-0001 C-5 裁定新增的。`get_runtime_events` 是否成为第 6 个只读工具留待 M4 决定；M2.5 只负责提供数据面。

部署记录只暴露**变更的配置键名**，不暴露值——配置值可能是凭据（威胁 T-3）。M0 场景 S3（错误配置发布）的必需证据是"`auth.audience` 发生了变更"，键名足够。

### 6. 剧本的生命周期与并发

- `POST /v1/scenarios/{id}/start` 记录 T0，返回 `run_token`。
- 同一服务同时被两个剧本作用 → **拒绝**（409），不定义合成规则。混合特征无法解释，也无法作为评测输入。
- `POST /v1/scenarios/stop` 清除激活状态。
- 未启动任何剧本时，查询接口返回**基线数据**（不是空、不是错误）。基线本身也由 seed 决定，因此同样可复现。

**为什么拒绝而不是合成**：两个故障叠加后的信号形态没有唯一正确答案，让 Agent 去诊断一个我自己都说不清的输入，得到的成功率数字没有意义。

### 7. 注入载荷的植入方式

剧本可以在 `logs` 段声明 `injected_lines`，内容为任意文本，原样写入日志流。M0 场景 S6 的四种注入形态（直接指令式、伪装系统消息、中文诱导、跨租户诱导）都通过这个机制表达。

synthetic-lab **不对注入文本做任何处理或转义**——它是不可信数据源的模拟器，处理责任在 Agent Runtime 侧（威胁 T-1 的缓解落在 M4）。如果 synthetic-lab 帮忙过滤了，M6 的注入测试就变成了自欺。

## Consequences

**正面**：

- 剧本可版本化、可 diff、可在 Gate 报告里贴出，满足 M6 冻结 dataset 的要求。
- seed 驱动使"连续 3 次一致"可用 SHA-256 严格验证，而非目测。
- 查询即时计算，评测 30~50 个 case 不需要等待真实时间流逝。

**负面 / 代价**：

- 剧本表达能力受 `shape` 词表限制，新形态需要改代码扩词表。
- 这不是真实的性能压力环境。真实资源压力（OOMKilled 的实际发生、CPU throttling）在 M8 用 Kubernetes 演练，M2.5 只提供"事件已发生"的可查询记录。**不得把 M2.5 的 OOMKilled 事件说成真实的内存耗尽验证。**
- 拒绝剧本叠加意味着无法构造"多重故障"场景。若 M6 需要，要回到本 ADR 重新裁决。

## 验证方式

1. 同一剧本 + 同一窗口连续查询 3 次，时间戳归一化后返回体 SHA-256 相同（M2.5 Gate 硬条件）。
2. 未启动剧本时查询返回基线数据，非空非错。
3. 剧本参数非法（未知服务、负窗口）→ typed failure，不静默用默认值。
4. 同一服务的第二个剧本 → 409。
5. 查询窗口超出剧本覆盖范围 → 返回可识别的"无数据"，不插值。
6. OOMKilled 剧本的重启事件与内存触顶时间戳对齐。
7. 注入文本被原样取回，未做任何过滤。
8. 部署记录不含配置值。
