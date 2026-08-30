# M1 Gate 自检报告

- 里程碑：**M1 — Java Control Plane**
- 报告日期：2026-08-26
- 结论：**M1 Gate 三条件全部通过（有真实输出）。**
- 上游依据：说明书 §23 M1，DEV_PROMPT §12 M1

---

## 1. Gate 条件逐条自评

DEV_PROMPT §12 M1 Gate 原文：**「空环境（干净 MySQL/Redis）能启动；成功路径和失败路径 API 都可重复复现；集成测试真实连库，不是 mock。」**

| # | 条件 | 自评 | 证据 |
|---|---|---|---|
| G1 | 空环境（干净 MySQL/Redis）能启动 | **通过** | `docker compose down -v` 后 `up -d`，三个容器全部 healthy（§3.3）。Flyway 从零建表，无需手工 SQL |
| G2 | 成功路径和失败路径 API 都可重复复现 | **通过** | `scripts/smoke-m1.sh` 对运行中服务打真实 HTTP，26 项断言全绿（§3.2）。每条失败路径断言具体状态码与错误码 |
| G3 | 集成测试真实连库，不是 mock | **通过** | Testcontainers 起真实 mysql:8.0 与 redis:7-alpine。无 H2、无 mock。77 测试全绿（§3.1） |
| G4 | `arguments_digest` 校验逻辑此时落地（M1 要点） | **通过** | ADR-0002 + ArgumentsCanonicalizer + 11 条审批安全测试 + 5 条 HTTP 层篡改测试 |
| G5 | 审计事件不可篡改追加 | **通过** | 数据库触发器强制；测试绕过应用层直接用 JDBC 尝试 UPDATE/DELETE，均被拒（§3.1 AuditImmutability） |
| G6 | 乐观锁用 Incident.version | **通过** | 8 线程并发更新，恰好 1 成功 7 冲突（§3.1 Concurrency） |

---

## 2. 交付物清单

### 2.1 决策文档

| 文件 | 内容 |
|---|---|
| [docs/adr/ADR-0002-arguments-digest.md](../adr/ADR-0002-arguments-digest.md) | 参数规范化与摘要算法（M0 Gate 报告 §5.1 B3 的阻塞项） |

### 2.2 契约

| 文件 | 内容 |
|---|---|
| [contracts/http/control-plane-v1.yaml](../../contracts/http/control-plane-v1.yaml) | OpenAPI 3.1，13 个端点、12 个错误码、完整 schema。跨语言边界的唯一依据 |

### 2.3 数据库

| 文件 | 内容 |
|---|---|
| `V1__init_schema.sql` | 11 张表：tenant、principal、incident、agent_run、run_terminal_state、run_step、approval、evidence_reference、checkpoint、audit_event、idempotency_record |
| `V2__audit_append_only.sql` | 4 个触发器，强制 audit_event 与 run_terminal_state 不可改写 |

### 2.4 Java 源码（30 个文件）

安全与审批核心：

| 文件 | 职责 |
|---|---|
| `approval/ArgumentsCanonicalizer.java` | RFC 8785 JCS 受限子集 + SHA-256 |
| `service/ApprovalService.java` | 审批权威判定点。request / decide / verifyAndConsume 三段分离 |
| `security/SecretRedactor.java` | 出口集中脱敏（T-3） |
| `security/TokenHasher.java` | token 只存 SHA-256 摘要 |
| `security/AuthenticationService.java` | Bearer token 认证 |
| `api/AuthenticatedCallerArgumentResolver.java` | tenantId 只从认证身份派生，不读请求参数 |
| `audit/AuditService.java` | REQUIRES_NEW 事务，拒绝事件不随调用方回滚消失 |
| `ratelimit/RedisRateLimiter.java` | Lua 原子固定窗口 |

其余：4 个 Controller、3 个 Service、6 个 Mapper、7 个领域模型、1 个全局异常映射、2 个配置类、1 个开发种子。

### 2.5 部署与脚本

| 文件 | 内容 |
|---|---|
| `apps/control-plane-java/Dockerfile` | 多阶段构建，非 root 运行 |
| `deploy/compose/docker-compose.yml` | MySQL 8 + Redis 7 + Control Plane，带 healthcheck 与依赖顺序 |
| `scripts/smoke-m1.sh` | 26 项真实 HTTP 断言 |

---

## 3. 真实运行输出

### 3.1 集成测试（Testcontainers，真 MySQL 8 + 真 Redis 7）

```
$ cd apps/control-plane-java && mvn test

[INFO] Tests run: 6,  Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 10.88 s -- TenantIsolationIntegrationTest
[INFO] Tests run: 12, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.007 s -- SecretRedactorTest
[INFO] Tests run: 5,  Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 1.356 s -- RateLimitIntegrationTest
[INFO] Tests run: 4,  Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.071 s -- AuditImmutabilityIntegrationTest
[INFO] Tests run: 7,  Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.402 s -- ConcurrencyAndTerminalStateIntegrationTest
[INFO] Tests run: 15, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 1.066 s -- HttpApiIntegrationTest
[INFO] Tests run: 11, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.522 s -- ApprovalSecurityIntegrationTest
[INFO] Tests run: 17, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.008 s -- ArgumentsCanonicalizerTest
[INFO] Tests run: 77, Failures: 0, Errors: 0, Skipped: 0
[INFO] BUILD SUCCESS
```

### 3.2 冒烟测试（真实 HTTP，打包镜像 + 真实中间件）

```
$ bash scripts/smoke-m1.sh

== 健康与认证 ==
ok    健康检查无需认证                             [200]
ok    无凭据访问 -> 401                            [401]
ok    伪造 token -> 401                            [401]

== RBAC ==
ok    VIEWER 创建 Incident -> 403                  [403]

== 成功路径：Incident -> Run -> Approval -> Consume ==
ok    OPERATOR 创建 Incident -> 201                [201]
ok    AGENT_RUNTIME 创建 Run -> 201                [201]
ok    发起审批请求 -> 201                          [201]
      argumentsDigest=3c9767a24af4f92ff64ec6f812e42edd4391267f00e14d0a3f65cd5c4912a062
ok    AGENT_RUNTIME 自批 -> 403（INV-4）           [403]
ok    未批准即消费 -> 403                          [403]
ok    APPROVER 批准 -> 200                         [200]

== 威胁 T-2：审批后篡改参数 ==
ok    改 service 后消费 -> 403                     [403]
ok    改 toolName 后消费 -> 403                    [403]
ok    改 resourceRef 后消费 -> 403                 [403]
ok    键顺序不同但语义相同 -> 200（不误拒）        [200]
ok    重放已消费的审批 -> 403                      [403]

== ADR-0002：浮点参数被拒绝 ==
ok    arguments 含浮点 -> 400                      [400]

== 唯一终态（INV-5）：同一 Run 结算三次 ==
ok    第一次结算 -> 200                            [200]
ok    第二次结算（换成 FAILED）-> 200              [200]
ok    第三次结算 -> 200                            [200]
ok    三次结算只有第一次生效，终态未被改写

== 乐观锁 ==
ok    version=1 更新 -> 200                       [200]
ok    重用 version=1 -> 409                       [409]

== 审计 ==
ok    审计事件可查                                 [200]
ok    拒绝类审计事件已记录（DENIED=30）

== Secret 不泄漏 ==
ok    401 响应未回显 token

===============================
PASS=26  FAIL=0
EXIT=0
```

### 3.3 空环境启动

```
$ cd deploy/compose && docker compose down -v --remove-orphans && docker compose up -d

$ docker compose ps --format 'table {{.Name}}\t{{.Status}}'
NAME               STATUS
rg-control-plane   Up 45 seconds (healthy)
rg-mysql           Up 55 seconds (healthy)
rg-redis           Up 55 seconds (healthy)
```

数据卷被 `-v` 删除，MySQL 从空库启动，Flyway 自动执行 V1 与 V2。

---

## 4. 开发过程中真实踩到的问题

按铁律三，这些不隐藏——它们是设计缺陷被测试逮住的记录：

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | Flyway V2 失败，MySQL ERROR 1419 | binlog 开启时创建触发器需要 SUPER 权限 | 容器加 `--log-bin-trust-function-creators=1`，测试与 compose 两处一致 |
| 2 | `Authorization: Bearer xxx` 只遮住 "Bearer"，token 明文漏出 | 脱敏模式顺序错：通用 `key=value` 先匹配，把 Authorization 当键、Bearer 当值 | 具体模式前置，通用模式最后 |
| 3 | JSON 形态 `"api_key": "xxx"` 未被遮盖 | 正则未允许键名后的引号 | 键名后加 `"?` |
| 4 | `ArgumentsCanonicalizer` 注入失败 | 它是纯函数类，故意不加 `@Component`（单测要能直接 new），但也忘了注册 Bean | 在 `ControlPlaneConfig` 用 `@Bean` 注册 |
| 5 | 从 `readOnly` 事务记审计报 "Connection is read-only" | `allowed()/denied()` 内部调用 `record()` 是自调用，走不到代理，`REQUIRES_NEW` 不生效 | 两个方法各自标注 `REQUIRES_NEW` |
| 6 | 并发结算终态死锁（`DeadlockLoserDataAccessException`） | `run_terminal_state` → `agent_run` 外键让 INSERT 先拿父行共享锁，随后 UPDATE 要升级为排他锁，两事务互等 | 结算前先 `SELECT ... FOR UPDATE` 锁父行，把结算串行化 |
| 7 | 主键冲突后读既有终态返回 null | REPEATABLE READ 下普通 SELECT 读事务开始时的快照，看不到并发赢家刚提交的行 | 新增 `findTerminalStatusFresh()` 用 `FOR SHARE` 锁定读 |
| 8 | Docker 构建失败：`eclipse-temurin:17-jre-alpine` | 该 tag 无 linux/arm64 manifest | 换 `17-jre`（Debian 基础） |
| 9 | compose 起不来：3306 端口被占 | 开发机已有本地 MySQL | 默认改 3307/6380，控制面走 compose 网络不受影响 |
| 10 | 冒烟脚本 `unbound variable` | bash 3.2 下 `$VAR（` 的全角括号被当成变量名一部分 | 改用 `${VAR}` |

第 6、7 两条值得单独说：它们只在**并发**测试下暴露，顺序调用两次完全正常。这正是 M0 把 A4（基础设施故障）当作威胁而非意外的原因——如果只测顺序路径，M2 的 Lease 接管上线后会以"偶发死锁 + 终态丢失"的形式在生产暴露。

---

## 5. 已知限制与未验证项

### 5.1 已知限制（设计决策，非缺陷）

1. **工具参数不能用浮点数**（ADR-0002）。M4 的工具 Schema 必须用整数或字符串表达小数，例如限流百分比用基点（`1250` = 12.50%）。
2. **实现的是 RFC 8785 的子集**，不是完整实现（浮点被拒绝而非按 ES6 序列化）。不得对外声称"实现了 RFC 8785"。
3. **不做空值折叠**：`{"a":null}` 与 `{}` 摘要不同。调用方需保证参数构造稳定，M4 应从 Schema 默认值统一填充。
4. **Secret 脱敏基于模式匹配**，无法覆盖任意格式凭据。它是纵深防御的一层，真正的防线是 Secret 只从环境变量注入。
5. **限流是固定窗口**，窗口边界处可能放过接近 2 倍配额。目的是防失控循环，不是精确整形。
6. **租户无物理隔离**，靠应用层 binding + 查询条件过滤。
7. **`decide` 与 `verifyAndConsume` 之间没有二次人工确认**：审批人批准后，Agent Runtime 可在 TTL 内任意时刻执行。TTL 默认 15 分钟。
8. **开发种子数据使用固定 token**，仅在 `RUNBOOKGUARD_SEED_DEV_DATA=true` 时写入。任何真实部署不得打开。

### 5.2 未验证项

| 项 | 状态 |
|---|---|
| Python 侧规范化实现与 Java 侧一致 | **未验证**。跨语言测试向量 `contracts/tools/digest-test-vectors.json` 尚未生成，M4 落地 Python 时必须做 |
| M0 五个正式阈值 | **未验证**，无 Agent 实现 |
| Lease / Heartbeat / 消息重投 | **未验证**，M2 交付 |
| Checkpoint 恢复与版本兼容判定 | **未验证**，M4 交付 |
| 干净机器上的一键复现 | **部分验证**。本机 `compose up` 成功，但未在另一台机器验证 |
| OpenTelemetry 接入 | **未做**，M7 |
| S0 学习计划状态 | `UNKNOWN`（M0 Gate 报告 Q1，仍未回答） |

---

## 6. 下一里程碑（M2：异步 Worker）的前置依赖

M2 交付：RabbitMQ 契约（`contracts/events/`）、Lease / Heartbeat / Cancel、幂等与死信队列、Worker kill 恢复演练。

### 6.1 M1 已为 M2 铺好的地基

| M1 产物 | M2 如何使用 |
|---|---|
| `run_terminal_state` 主键 + 触发器 | Lease 接管后两个 Worker 竞争结算，由数据库裁决唯一赢家 |
| `settleTerminal` 幂等语义（`firstSettlement`） | 消息重投三次时消费者可安全重复调用 |
| `idempotency_record` 表（已建，M1 未使用） | M2 的消息幂等键落点 |
| `lockForSettlement`（`FOR UPDATE`） | 避免并发接管时死锁 |
| 乐观锁 `version` | Worker 推进状态时检测并发冲突 |

### 6.2 M2 应先写的失败测试

1. 同一消息投递三次 → 只有一个终态、副作用只发生一次。
2. Worker 在写动作提交前被 `kill -9` → Lease 过期后另一 Worker 接管 → 唯一终态。
3. Lease 未过期时第二个 Worker 尝试接管 → 被拒。
4. Heartbeat 停止 → Lease 过期 → 任务可被接管。
5. 消息反复失败 → 进入死信队列，不无限重试。
6. Cancel 请求 → Worker 在下一个检查点停止，且落 FAILED 终态而非悬挂。
7. 消费者处理中进程退出 → 消息不丢（手动 ack）。

### 6.3 M2 需要决策的事项

| 事项 | 说明 |
|---|---|
| Lease 存储位置 | Redis（TTL 天然契合）vs MySQL（与业务事实同事务）。倾向 MySQL 持有权威 Lease、Redis 做加速，需 ADR-0003 |
| 幂等键构造规则 | 从消息属性派生还是由生产者指定 |
| 死信队列重试策略 | 最大重试次数、退避曲线 |
| Cancel 的传播方式 | 数据库标记轮询 vs 独立取消队列 |

---

## 7. 可复现命令

```bash
cd /Users/lijunpeng/Desktop/open_source_project/RunbookGuard-Agent-Platform

# 单元 + 集成测试（自动拉起 Testcontainers，需要 Docker）
cd apps/control-plane-java && mvn test

# 只跑摘要算法单测（不需要 Docker）
mvn test -Dtest=ArgumentsCanonicalizerTest

# 空环境启动
cd ../../deploy/compose
docker compose down -v --remove-orphans
docker compose up -d --build
docker compose ps

# 冒烟测试（需要栈已 healthy）
cd ../..
bash scripts/smoke-m1.sh

# 清理
cd deploy/compose && docker compose down -v
```

---

## 8. 简历状态

**仍不允许写入简历。** M6 Gate 未通过，所有能力指标为 `未验证`。M1 完成的是"业务事实的权威持有者"，不是产品能力证明。
