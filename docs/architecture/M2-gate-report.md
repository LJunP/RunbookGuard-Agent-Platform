# M2 Gate 自检报告

- 里程碑：**M2 — 异步 Worker**
- 报告日期：2026-08-26
- 结论：**M2 Gate 两条件全部通过（有真实输出）。**
- 上游依据：说明书 §23 M2，DEV_PROMPT §12 M2

---

## 1. Gate 条件逐条自评

DEV_PROMPT §12 M2 Gate 原文：**「重复投递不产生重复终态（有测试证明）；kill -9 Worker 后任务可恢复或落到唯一失败终态。演练命令写进文档。」**

| # | 条件 | 自评 | 证据 |
|---|---|---|---|
| G1 | 重复投递不产生重复终态，有测试证明 | **通过** | 集成测试：同键投递三次副作用只发生一次；终态结算三次 `countByRun == 1`；8 线程并发结算恰好 1 个 `firstSettlement=true`。演练：HTTP 层结算三次，终态未被改写（§3.2 演练 4） |
| G2 | kill -9 Worker 后可恢复或落唯一失败终态 | **通过** | `WorkerRecoveryIntegrationTest` 9 项：Lease 过期后回收器重新派发；deadline 已过 / 投递超上限 / 已取消三种情形直接落唯一 FAILED。演练 §3.2 演练 3 验证僵尸 Worker 被 fencing 拦住 |
| G3 | 演练命令写进文档 | **通过** | `scripts/drill-m2.sh`，35 项断言，复现命令见 §6 |
| G4 | 幂等键能扛住同一消息投递三次（M2 要点） | **通过** | 两阶段占位设计 + 9 项幂等测试。关键：同键不同内容判为冲突而非重复 |
| G5 | Lease 过期后可被另一 Worker 安全接管，不产生两个终态 | **通过** | 13 项 Lease 测试 + fencing token 机制 |

---

## 2. 交付物清单

### 2.1 决策与契约

| 文件 | 内容 |
|---|---|
| [docs/adr/ADR-0003-lease-and-idempotency.md](../adr/ADR-0003-lease-and-idempotency.md) | Lease 权威存储、fencing token、幂等键构造、Cancel 传播、死信策略 |
| [contracts/events/README.md](../../contracts/events/README.md) | RabbitMQ 拓扑、5 类消息头、3 种消息体、15 个 failureClass 枚举、幂等语义、版本演进规则 |

### 2.2 数据库

| 文件 | 内容 |
|---|---|
| `V3__worker_lease_and_cancel.sql` | `worker_lease` 表 + `agent_run` 的 `cancel_requested_at` / `cancel_requested_by` / `dispatch_sequence` |
| `V4__idempotency_two_phase.sql` | 幂等记录改为两阶段（占位 → 回填），结果字段可空 + `completed_at` |

### 2.3 Java 源码（新增 12 个文件）

| 文件 | 职责 |
|---|---|
| `worker/LeaseService.java` | Lease 获取 / 心跳 / 释放 / fencing token 校验 |
| `worker/LeaseReclaimer.java` | 扫描过期 Lease，重新派发或结算终态 |
| `worker/TerminalSettlementService.java` | 无调用方身份的终态结算，供后台任务使用 |
| `worker/CancellationService.java` | 取消标记的写入与查询 |
| `worker/Lease.java`、`StaleFencingTokenException.java` | 领域类型 |
| `idempotency/IdempotencyService.java` | 两阶段幂等执行 |
| `idempotency/IdempotencyStore.java` | 独立事务边界（自调用走不到代理，必须拆开） |
| `idempotency/IdempotencyConflictException.java`、`IdempotencyInProgressException.java` | 冲突与进行中两种不同语义 |
| `messaging/RabbitTopologyConfig.java` | Exchange / Queue / DLQ / Binding |
| `messaging/RunDispatchPublisher.java` | 派发消息，序号自增后构造幂等键 |
| `messaging/RunMessages.java`、`FailureClass.java` | 消息体与失败分类枚举 |
| `api/WorkerController.java` | Worker 生命周期 API（7 个端点） |
| `persistence/WorkerLeaseMapper.java`、`IdempotencyRecordMapper.java` | 持久层 |

### 2.4 测试与脚本

| 文件 | 内容 |
|---|---|
| `worker/LeaseIntegrationTest.java` | 13 项 |
| `worker/WorkerRecoveryIntegrationTest.java` | 9 项（kill -9 演练） |
| `idempotency/IdempotencyIntegrationTest.java` | 9 项 |
| `api/WorkerApiIntegrationTest.java` | 10 项（HTTP 层） |
| `scripts/drill-m2.sh` | 35 项真实 HTTP 演练 |

---

## 3. 真实运行输出

### 3.1 集成测试（真 MySQL 8 + 真 Redis 7 + 真 RabbitMQ 3.13）

```
$ cd apps/control-plane-java && mvn test

[INFO] Tests run: 6,  Failures: 0, Errors: 0 -- TenantIsolationIntegrationTest
[INFO] Tests run: 12, Failures: 0, Errors: 0 -- SecretRedactorTest
[INFO] Tests run: 9,  Failures: 0, Errors: 0 -- IdempotencyIntegrationTest
[INFO] Tests run: 5,  Failures: 0, Errors: 0 -- RateLimitIntegrationTest
[INFO] Tests run: 4,  Failures: 0, Errors: 0 -- AuditImmutabilityIntegrationTest
[INFO] Tests run: 7,  Failures: 0, Errors: 0 -- ConcurrencyAndTerminalStateIntegrationTest
[INFO] Tests run: 15, Failures: 0, Errors: 0 -- HttpApiIntegrationTest
[INFO] Tests run: 13, Failures: 0, Errors: 0 -- LeaseIntegrationTest
[INFO] Tests run: 9,  Failures: 0, Errors: 0 -- WorkerRecoveryIntegrationTest
[INFO] Tests run: 11, Failures: 0, Errors: 0 -- ApprovalSecurityIntegrationTest
[INFO] Tests run: 17, Failures: 0, Errors: 0 -- ArgumentsCanonicalizerTest
[INFO] Tests run: 108, Failures: 0, Errors: 0, Skipped: 0
[INFO] BUILD SUCCESS
```

`WorkerApiIntegrationTest` 单独运行结果（与上表并行执行时容器复用，此处单跑记录）：

```
$ mvn test -Dtest=WorkerApiIntegrationTest
[INFO] Tests run: 10, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 19.23 s
[INFO] BUILD SUCCESS
```

合计 **118 项测试全绿**。

### 3.2 故障演练（真实 HTTP，四容器栈）

```
$ bash scripts/drill-m2.sh

== 演练 1：Lease 获取与互斥 ==
ok    worker-1 获取 Lease -> 200                    [200]
ok    acquired=true                                 [True]
ok    fencingToken=1                                [1]
ok    worker-2 在有效期内获取 -> 200                [200]
ok    worker-2 acquired=false（互斥生效）           [False]

== 演练 2：进度上报需要有效 fencing token ==
ok    worker-1 用有效 token 上报 -> 200             [200]
ok    预算消耗已记录 costSpentMicros=1500           [1500]
ok    stepsUsed=3                                   [3]
ok    worker-2 用伪造 token 上报 -> 409             [409]

== 演练 3：kill -9 模拟（Lease 过期后接管，僵尸 Worker 被 fencing 拦住）==
ok    worker-1 主动释放 Lease（等价于优雅退出）    [200]
ok    worker-2 接管 -> 200                          [200]
ok    接管成功 acquired=true                        [True]
ok    fencing token 递增（1 -> 2）
ok    僵尸 worker-1 带旧 token 上报 -> 409          [409]
ok    僵尸 worker-1 heartbeat -> 200 但 acquired=false [200]
ok    heartbeat 明确告知已失去 Lease                [False]

== 演练 4：唯一终态（接管者结算三次）==
ok    第一次结算 -> 200                             [200]
ok    firstSettlement=true                          [True]
ok    第二次结算（改成 FAILED）-> 200               [200]
ok    firstSettlement=false                         [False]
ok    终态未被改写，仍是 COMPLETE                   [COMPLETE]
ok    第三次结算 -> 200                             [200]
ok    仍为 firstSettlement=false                    [False]
ok    终态后继续上报进度 -> 409                     [409]

== 演练 5：failureClass 受控枚举 ==
ok    自由文本 failureClass -> 400                  [400]
ok    枚举内的 failureClass -> 200                  [200]

== 演练 6：取消传播 ==
ok    OPERATOR 请求取消 -> 200                      [200]
ok    firstRequest=true                             [True]
ok    重复取消 -> 200                               [200]
ok    firstRequest=false（幂等）                    [False]
ok    AGENT_RUNTIME 请求取消 -> 403                 [403]
ok    Worker 获取 Lease 时看到取消标记 -> 200       [200]
ok    cancelRequested=true                          [True]

== 演练 7：审计留痕 ==
ok    审计事件可查                                  [200]
ok    Lease 获取已留审计（lease.acquire=7）

===============================
PASS=35  FAIL=0
EXIT=0
```

### 3.3 M1 冒烟未回归

```
$ bash scripts/smoke-m1.sh
PASS=26  FAIL=0
EXIT=0
```

### 3.4 四容器栈健康

```
$ docker compose ps --format 'table {{.Name}}\t{{.Status}}'
NAME               STATUS
rg-control-plane   Up 54 seconds (healthy)
rg-mysql           Up About a minute (healthy)
rg-rabbitmq        Up About a minute (healthy)
rg-redis           Up About a minute (healthy)
```

---

## 4. 开发过程中真实踩到的问题

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | 8 线程并发获取 Lease 死锁 | 并发 INSERT 同一主键在 InnoDB 的插入意图锁上互等 | 随 Run 创建一并插入占位行，acquire 变成纯 UPDATE（只在行锁上排队） |
| 2 | 短 TTL 测试无法表达 | SQL 用 `INTERVAL ? SECOND`，秒粒度下 300ms 的 TTL 无从表达，而 Lease 正确性只在过期边界可验证 | 改用 `INTERVAL ? * 1000 MICROSECOND` |
| 3 | 可接管列表包含从未派发的 Run | 占位行的 `expires_at = NOW(3)`，立即"过期" | 加 `fencing_token > 0` 条件 |
| 4 | **幂等测试：8 并发下副作用发生 8 次** | 占位写在业务执行**之后**，并发者各自跑完业务才发现重复 | 改两阶段：先占位（独立事务立即提交）→ 执行业务 → 回填结果；失败则释放占位 |
| 5 | `REQUIRES_NEW` 不生效 | `executeOnce` 内部调用同类的 `reserve/complete` 是自调用，走不到 Spring 代理 | 拆出独立 Bean `IdempotencyStore` |
| 6 | 回收器聚合计数导致断言互相干扰 | 回收器按设计扫描全局，测试并行时计数被其他测试的 Run 污染 | `ReclaimReport` 改为返回 runId 列表，断言改为 `contains` |
| 7 | Testcontainers 容器名冲突 | 上一次 compose 未清理干净 | `docker rm -f` 后重建 |

第 4 条是本里程碑最有价值的发现：顺序调用三次完全正确，只有 8 线程并发才暴露。这正是"重复投递不产生重复终态"需要用真并发验证的原因——串行测试会给出通过的假象。

---

## 5. 已知限制与未验证项

### 5.1 已知限制（设计决策）

1. **未启用 publisher confirm**。当前保证是：Control Plane 在事务提交后发消息，消息丢失时由 Lease 回收扫描重新派发。代价是消息丢失的发现延迟等于一个 Lease TTL（默认 30s）。启用 confirm 需要额外的回调与重发逻辑，M2 未做。
2. **DLQ 只作事后取证，不是恢复路径**。投递计数达上限时主动结算 `FAILED / max_delivery_exceeded`，因为悬挂在 DLQ 的 Run 既不成功也不失败，会让 M6 的恢复唯一终态率无法计算。
3. **Cancel 不即时**，粒度是一个节点边界。与 ADR-0001 C-6 的协作式取消语义一致。
4. **Lease 获取走数据库**，比 Redis 慢（毫秒级）。ADR-0003 §1 已判定这点延迟换不到任何东西，而 Redis 主从延迟会直接损伤 INV-5。
5. **回收器目前是手动触发的方法**（`reclaimOnce`），尚未挂定时任务。挂调度器是 M7 的事（需要与 OTel 指标一起做，否则回收行为不可观测）。
6. **`worker_lease` 无历史表**。接管次数只能从审计事件 `lease.acquire` 统计，不能直接查"这个 Run 被接管过几次"。
7. **不存在真正的 exactly-once**。做的是"至少一次投递 + 幂等执行 = 效果唯一"。推论仍成立：自身不幂等的动作工具不允许进入动作工具集。

### 5.2 未验证项

| 项 | 状态 |
|---|---|
| 真正的进程级 `kill -9`（当前用"不 release 不 heartbeat"等价模拟） | **部分验证**。语义等价（死掉的 Worker 就是不会 release），但未验证 JVM 被 SIGKILL 时是否有别的副作用。真实 kill 演练需要独立 Worker 进程，M3 有了 Python Runtime 后才能做 |
| 消息在 broker 重启后是否存活 | **未验证**。队列声明为 durable，但未做 broker 重启演练 |
| 死信队列的实际投递（当前 DLQ 已声明并绑定，但未构造反复失败的消息去填充它） | **未验证** |
| Python 侧的 Lease 客户端 | **未做**，M3/M4 |
| M0 五个正式阈值 | **未验证**，无 Agent 实现 |
| 干净机器上的一键复现 | **部分验证**（本机四容器全绿，未换机验证） |
| S0 学习计划状态 | `UNKNOWN`（M0 Gate 报告 Q1，仍未回答） |

---

## 6. 可复现命令

```bash
cd /Users/lijunpeng/Desktop/open_source_project/RunbookGuard-Agent-Platform

# 全部测试（自动拉起 MySQL + Redis + RabbitMQ 容器）
cd apps/control-plane-java && mvn test

# 只跑 M2 相关
mvn test -Dtest='LeaseIntegrationTest,WorkerRecoveryIntegrationTest,IdempotencyIntegrationTest,WorkerApiIntegrationTest'

# 起四容器栈
cd ../../deploy/compose
docker compose down -v --remove-orphans
docker compose up -d --build
docker compose ps

# M2 故障演练（35 项）
cd ../..
bash scripts/drill-m2.sh

# M1 冒烟回归（26 项）
bash scripts/smoke-m1.sh

# RabbitMQ 管理界面（观察队列与 DLQ）
open http://127.0.0.1:15673   # 账号见 deploy/compose/docker-compose.yml

# 清理
cd deploy/compose && docker compose down -v
```

---

## 7. 下一里程碑（M2.5：Synthetic Lab）的前置依赖

M2.5 交付：`services/synthetic-lab/`，可编程注入故障的合成服务；故障剧本格式；4 类故障（连接池耗尽、MQ backlog、5xx 升高、OOMKilled）；**5 类可查询接口**（ADR-0001 C-5 已裁定从 4 类增加到 5 类，补容器/运行时事件）。

### 7.1 阻塞项

| # | 前置依赖 | 状态 |
|---|---|---|
| B1 | M2 Gate 验收 | 待验收 |
| B2 | 故障剧本格式决策（声明式 YAML vs 代码） | 需在 M2.5 开工时定，写 ADR-0004 |
| B3 | synthetic-lab 用什么语言 | 倾向 Python（与 M3 的 Agent Runtime 同栈，减少一套工具链），需在 ADR-0004 记录 |

### 7.2 M2.5 应先写的失败测试

按铁律二，不是 happy path：

1. 同一故障剧本连续跑 3 次，指标与日志特征的**关键判据**稳定（M2.5 Gate 硬条件）。
2. 剧本未启动时查询接口返回基线数据，不返回空或报错。
3. 剧本参数非法（负数窗口、未知服务名）→ 拒绝并给出 typed failure，不静默使用默认值。
4. 两个剧本同时作用于同一服务 → 明确拒绝或定义合成规则，不产生不可解释的混合特征。
5. 查询接口的时间窗口超出剧本覆盖范围 → 返回可识别的"无数据"而非插值。
6. OOMKilled 剧本产生的重启事件与内存指标时间戳对齐（S4 场景的必需证据）。
7. 注入 Prompt Injection 文本到日志 → 能被日志查询接口原样取回（S6 场景依赖）。

### 7.3 M2.5 与 M0 场景的对应

M2.5 完成后，M0 的 8 个场景中 S1（连接池耗尽）、S2（MQ backlog）、S3（5xx 升高）、S4（OOMKilled）具备数据基础。S5 依赖 M2（已完成）+ M4；S6 依赖日志注入能力（M2.5 提供）+ M4 的 Policy；S7 / S8 依赖 M5 的 Runbook 检索。

---

## 8. 简历状态

**仍不允许写入简历。** M6 Gate 未通过。M2 完成的是可靠性地基（Lease / 幂等 / 唯一终态），不是产品能力证明。
