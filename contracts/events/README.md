# RunbookGuard 事件契约 v1

跨语言消息契约。Java Control Plane 生产 dispatch 消息，Python Agent Runtime 消费；
Runtime 生产进度与终态消息，Control Plane 消费。两侧都必须校验同一份 schema。

相关决策：[ADR-0003](../../docs/adr/ADR-0003-lease-and-idempotency.md)

## 拓扑

```
                    exchange: rg.run (direct, durable)
                              |
        +---------------------+---------------------+
        | routing: run.dispatch                     | routing: run.progress
        v                                           v
  queue: rg.run.dispatch                      queue: rg.run.progress
  (durable, manual ack)                       (durable, manual ack)
        |                                           |
        | 失败 5 次后不进 DLQ 等人工，                | 同左
        | 由消费者主动结算 FAILED                     |
        v                                           v
  queue: rg.run.dispatch.dlq                  queue: rg.run.progress.dlq
  (仅作事后取证，不作为恢复路径)
```

DLQ 只用于事后取证。恢复路径是「达到投递上限 → 主动结算终态」，因为悬挂在 DLQ 里的 Run
既不是成功也不是失败，会让 M6 的恢复唯一终态率无法计算。

## 通用消息头

| 头 | 必需 | 说明 |
|---|---|---|
| `x-idempotency-key` | 是 | `{operation}:{run_id}:{sequence}`。由生产者构造，不由消费者推导 |
| `x-delivery-count` | 是 | 消费者维护并在重发时递增。不依赖 broker 的 `x-death`，因为 classic 与 quorum queue 行为不同 |
| `x-tenant-id` | 是 | 用于消费侧快速拒绝，**不作为授权依据**——授权仍从 Control Plane 的权威数据校验 |
| `x-trace-id` | 是 | 贯穿 HTTP → MQ → Agent node → tool call |
| `x-schema-version` | 是 | 本文档版本，当前 `1` |

`x-tenant-id` 存在的意义只是让消费者不必反序列化整个 body 就能路由/记日志。信任等级与
Tool result 相同：不可信数据。

## 消息类型

### RunDispatch（Control Plane → Agent Runtime）

routing key `run.dispatch`

```json
{
  "schemaVersion": 1,
  "runId": "run-3f2a...",
  "incidentId": "inc-948e...",
  "tenantId": "tenant-demo",
  "principalId": "prin-agent",
  "sequence": 1,
  "graphVersion": "g1",
  "promptVersion": "p1",
  "modelId": "fake-provider",
  "datasetVersion": "incidents-dev-v1",
  "maxSteps": 25,
  "deadline": "2026-08-26T13:10:00Z",
  "costBudgetMicros": 500000,
  "tokenBudget": 200000,
  "toolCallBudget": 20,
  "resumeFromCheckpointId": null
}
```

`sequence` 从 1 开始，同一 Run 每次 dispatch（含 Lease 接管后的重新派发）递增。
它与 `runId` 一起构成幂等键，两者都持久化在数据库中，因此生产者重启后仍能重现同一个键。

`resumeFromCheckpointId` 非 null 时，消费者必须先校验 checkpoint 的 `graph_version` 与
`state_schema_version` 兼容；不兼容走失败终态，**不硬恢复**（M0 终止条件 7）。

预算字段在消息里冗余一份，是为了让 Worker 不必先回查 Control Plane 就能开始有界执行。
权威值仍在 `agent_run` 表；两者不一致时以数据库为准。

### RunProgress（Agent Runtime → Control Plane）

routing key `run.progress`

```json
{
  "schemaVersion": 1,
  "runId": "run-3f2a...",
  "tenantId": "tenant-demo",
  "fencingToken": 3,
  "stepSequence": 7,
  "nodeName": "EXECUTING_TOOL",
  "status": "OBSERVE",
  "toolCallId": "tc-91b2...",
  "inputArtifact": "artifact://run-3f2a/step-7/in",
  "outputArtifact": "artifact://run-3f2a/step-7/out",
  "costSpentMicros": 12400,
  "tokenSpent": 8210,
  "toolCallCount": 3,
  "stepsUsed": 7,
  "occurredAt": "2026-08-26T13:02:11.482Z"
}
```

`fencingToken` 是必需的。Control Plane 校验它等于当前 Lease 的 token 才接受写入——
Lease 过期被接管后，原 Worker 可能从 GC 暂停中恢复并继续上报，那些上报必须被拒绝。

### RunTerminal（Agent Runtime → Control Plane）

routing key `run.progress`（与 progress 同队列，保证顺序）

```json
{
  "schemaVersion": 1,
  "runId": "run-3f2a...",
  "tenantId": "tenant-demo",
  "fencingToken": 3,
  "terminalStatus": "FAILED",
  "failureClass": "budget_exhausted",
  "occurredAt": "2026-08-26T13:05:00.000Z"
}
```

`terminalStatus` ∈ {COMPLETE, FAILED, BLOCKED}。

`failureClass` 在 `terminalStatus != COMPLETE` 时必填，取值受限于下表——自由文本会让 M6
的失败归因统计无法进行。

| failureClass | 对应 M0 终止条件 |
|---|---|
| `max_steps_exhausted` | 1 |
| `deadline_exceeded` | 2 |
| `cost_budget_exhausted` | 3 |
| `token_budget_exhausted` | 3 |
| `tool_call_budget_exhausted` | 4 |
| `repeated_state_detected` | 5 |
| `authorization_failed` | 6 |
| `version_incompatible` | 7 |
| `safety_rule_triggered` | 8 |
| `insufficient_evidence` | 安全停止（正确弃答，非缺陷） |
| `provider_failure` | 模型调用失败 |
| `tool_failure` | 工具执行失败 |
| `cancelled` | 人工取消 |
| `max_delivery_exceeded` | 消息重试超上限 |
| `internal_error` | 未分类，需要排查 |

`insufficient_evidence` 与 `COMPLETE` 都算成功行为：M0 场景 S7 / S8 要求证据不足时正确弃答。
终态记为 COMPLETE 且结论类型标注为 insufficient-evidence；这里的 failureClass 只在
Runtime 主动判定无法继续时使用。

### CancelRequest

不走消息队列。取消通过 `agent_run.cancel_requested_at` 传播，Worker 在节点边界轮询
（ADR-0003 §5）。原因是消息可能到达已不持有 Lease 的 Worker，或在重启后丢失。

## 幂等语义

消费者收到消息后：

1. 用 `x-idempotency-key` 查 `idempotency_record`。
2. 命中且 `request_digest` 相同 → 重复投递，直接返回首次结果并 ack。
3. 命中但 `request_digest` **不同** → 冲突，拒绝并告警。**这不是重复**：说明有人用同一个
   键提交了不同内容。把冲突当重复会静默丢弃第二个请求。
4. 未命中 → 正常处理，在同一事务内写入幂等记录。

`request_digest` 用 ADR-0002 的规范化算法对 payload 求摘要，因此消息体的键顺序与空白
不影响判定。

## 重试与退避

投递计数达到 5 → 消费者主动结算 `FAILED / max_delivery_exceeded`，然后 ack（不再重投）。

退避通过延迟重发实现：1s / 4s / 16s / 64s。不用 `x-message-ttl` + DLX 轮转，那种做法
无法在同一队列上按消息设置不同延迟。

## 版本演进

`x-schema-version` 与 body 内 `schemaVersion` 双写。消费者遇到不认识的版本：
拒绝 + 结算 `FAILED / version_incompatible`，不尝试猜测字段含义。

新增可选字段不升版本；删除字段、改字段语义、改枚举取值含义必须升版本。
