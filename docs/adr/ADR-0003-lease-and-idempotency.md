# ADR-0003：Lease 的权威存储与幂等键构造

- Status: Accepted
- Date: 2026-08-26
- 里程碑：M2（M1 Gate 报告 §6.3 的待决事项）
- 关联：[威胁模型 T-6](../architecture/M0-threat-model.md)、[ADR-0002](ADR-0002-arguments-digest.md)

## Context

M2 要交付 Lease / Heartbeat / Cancel、幂等、死信。三个决策必须先定，否则实现会在两处各写一半：

1. Lease 的权威状态放 Redis 还是 MySQL？
2. 幂等键由谁构造、覆盖什么？
3. Cancel 怎么传播到正在执行的 Worker？

背景约束（来自 M0）：INV-5 要求每个 Run 只有一个终态；T-6 要求崩溃后不重复副作用；说明书 §11 说 Redis 用于"限流、短期状态、**Lease 辅助**"——注意是辅助，不是权威。

## Decision

### 1. Lease 权威状态在 MySQL，Redis 不参与

`worker_lease` 表持有权威 Lease。**不使用 Redis 存 Lease**，连"加速层"也不做。

**理由**：Lease 的唯一作用是决定"谁有权推进这个 Run 并写终态"。这个判定必须与终态写入在**同一个事务边界**内可验证，否则会出现：Redis 说 Lease 归我 → 我去写终态 → 但另一个 Worker 因 Redis 主从延迟也认为 Lease 归它。Redis 的 TTL 语义很适合限流那种"错一点没关系"的场景，但不适合裁决唯一性。

放弃的收益：Redis 抢锁比数据库行锁快。但 Lease 获取是每个 Run 一次的低频操作（不是每次工具调用），这点延迟换不到任何东西。**过早优化会直接损伤 INV-5。**

Lease 过期判定用**数据库时间**（`expires_at <= NOW(3)`），不用应用进程时间。多个 Worker 的时钟不同步时，用应用时间会让两个 Worker 对"是否已过期"得出不同结论。

### 2. Lease 状态机与接管条件

```
无 Lease  --acquire-->  HELD(expires_at = now + ttl)
HELD      --heartbeat-->  HELD(expires_at 顺延)
HELD      --release-->  RELEASED
HELD      --过期-->  可被其他 Worker acquire（fencing_token + 1）
```

接管的 SQL 条件（单条语句完成判定与写入，避免"先查后写"的竞态）：

```sql
UPDATE worker_lease
SET owner_id = ?, expires_at = NOW(3) + INTERVAL ? SECOND,
    fencing_token = fencing_token + 1, acquired_at = NOW(3)
WHERE run_id = ? AND (owner_id = ? OR expires_at <= NOW(3))
```

`owner_id = ?` 分支让**原持有者重入**（同一 Worker 重试消息时不必等自己的 Lease 过期）。

### 3. Fencing token：防止"僵尸 Worker"写入

每次 Lease 被重新获取，`fencing_token` 自增。Worker 在推进状态或结算终态时必须携带自己持有的 token，服务端校验 token 等于当前值才放行。

**为什么需要**：Lease 过期不代表原 Worker 已经死了——它可能只是被 GC 停了 20 秒。Lease 被接管后原 Worker 恢复运行，此时它仍以为自己持有 Lease。没有 fencing token，它会带着过期的认知去写状态。这是分布式锁的经典失效模式，**仅靠 TTL 无法防护**。

M1 已有的终态主键约束是最后一道防线（保证不出现两个终态），但 fencing token 让僵尸写入在更早的位置被拒绝，且能留下审计。

### 4. 幂等键：由生产者构造，覆盖"业务意图"

格式：`{operation}:{run_id}:{sequence}`，例如 `run.dispatch:run-abc:1`。

- **由生产者构造并放入消息头 `x-idempotency-key`**，不由消费者从消息内容推导。消费者推导会在消息体多一个空格时算出不同的键。
- **不使用 RabbitMQ 的 `message_id`**：重投时 broker 可能生成新 id，且它表达的是"这条消息"而不是"这个业务意图"。两次不同的消息如果表达同一意图，必须共享幂等键。
- 幂等记录落 `idempotency_record` 表（M1 已建），带 `request_digest`（用 ADR-0002 的规范化算法对 payload 求摘要）。

**键相同但摘要不同 = 冲突，不是重复**：这说明有人用同一个幂等键提交了不同内容，必须拒绝并告警，而不是返回第一次的结果。这是幂等实现最容易做错的地方——把冲突当重复会静默丢弃第二个请求。

### 5. Cancel：数据库标记 + Worker 在检查点轮询

`agent_run` 增加 `cancel_requested_at`。Worker 在每个节点边界检查该字段，命中则停止并结算 `FAILED` / `failure_class=cancelled`。

**不用独立取消队列**：队列消息可能到达一个已经不持有 Lease 的 Worker，或在 Worker 重启后丢失。数据库标记是持久的，且接管者能立即看到。

代价：取消不是即时的，粒度是一个节点。对本项目可接受——节点边界的间隔由工具 timeout 决定（秒级）。ADR-0001 C-6 已经定了只读工具是协作式取消，这里保持一致。

### 6. 死信：最多 5 次投递后进 DLQ，指数退避

- 主队列 `rg.run.dispatch`，绑定 `rg.run.dispatch.dlx`。
- 消费者手动 ack。业务异常 → `nack(requeue=false)` 进 DLQ 前先递增投递计数。
- 投递计数存在消息头 `x-delivery-count`（由消费者维护并重新发布，不依赖 broker 的 `x-death`，因为 quorum queue 与 classic queue 的行为不同）。
- 达到 5 次 → 直接结算 `FAILED` / `failure_class=max_delivery_exceeded`，**不留在 DLQ 等人工**。

**为什么达到上限要主动结算终态**：一个卡在 DLQ 里的 Run 会永远停在非终态，M6 的"恢复唯一终态率"指标就无法计算（既不是成功也不是失败，是悬挂）。悬挂状态是最难排查的故障形态。

退避：1s / 4s / 16s / 64s，通过延迟重发实现（不用 `x-message-ttl` + DLX 轮转，那种做法在同一队列上难以按消息设置不同延迟）。

## Consequences

**正面**：

- Lease 判定与终态写入在同一事务域内，INV-5 有单一裁决点。
- Fencing token 让僵尸 Worker 的写入在应用层被拒绝并审计，而不是撞到主键约束才发现。
- 达到重试上限主动结算终态，消除悬挂状态。

**负面 / 代价**：

- Lease 获取走数据库，比 Redis 慢（毫秒级 vs 亚毫秒级）。已判定为不重要。
- Cancel 有最多一个节点的延迟。
- 幂等键由生产者构造，意味着生产者必须能稳定地重现同一个键——如果生产者自己重启后用新键重发，幂等失效。M2 的 dispatch 键从 `run_id + sequence` 派生，两者都持久化在数据库里，可重现。

## 验证方式

1. 同一消息投递三次 → 终态记录数 == 1，副作用计数 == 1。
2. Lease 未过期时第二个 Worker acquire → 返回 false。
3. Lease 过期后接管 → fencing_token 自增；原持有者带旧 token 写入 → 被拒 + 审计。
4. `kill -9` Worker → Lease 过期 → 接管 → 唯一终态。
5. 同一幂等键 + 不同 payload → 冲突异常，不返回首次结果。
6. 连续失败 5 次 → 结算 FAILED / max_delivery_exceeded，不悬挂。
7. Cancel 标记 → 下一个检查点停止 → FAILED / cancelled。
