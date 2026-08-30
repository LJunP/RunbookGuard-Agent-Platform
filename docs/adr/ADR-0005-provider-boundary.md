# ADR-0005：Provider 抽象边界与失败语义

- Status: Accepted
- Date: 2026-08-27
- 里程碑：M3（M2.5 Gate 报告 §6.1 的待决事项 B2）
- 关联：[威胁模型 T-3 / T-5](../architecture/M0-threat-model.md)、[事件契约 failureClass](../../contracts/events/README.md)

## Context

M3 的 Gate 条件只有一句：**模型失败（超时、返回垃圾、限流）不能产生假成功，每种失败都有对应测试。**

这句话决定了 Provider 层的全部设计。要满足它，必须先回答三个边界问题：

1. Provider 负责到哪一层？它要不要理解业务 schema？
2. 什么算"失败"，失败怎么表达？
3. fake provider 怎么做才不会变成"自己出题自己判卷"？

第 3 条是最容易做错的。fake provider 是我写的，它的返回结构、字段名、出错方式全都是我预设的，所以 Adapter 必然能处理它。**如果 fake 只会返回完美的响应，那么基于 fake 的测试全绿不构成任何证明。**

## Decision

### 1. Provider 只管"拿到文本 + 用量"，不理解业务 schema

Provider 的职责边界：

| Provider 负责 | Provider 不负责 |
|---|---|
| HTTP 调用、鉴权头、超时 | 业务 schema 校验 |
| 重试与退避（仅可重试错误） | Prompt 构造 |
| 解析 provider 自身的响应包络（choices / usage） | 工具调用的授权判定 |
| 上报 token 用量与成本 | 预算是否超限的裁决 |
| 出口脱敏 | Checkpoint / 状态 |

**为什么不把 schema 校验放进 Provider**：一旦 Provider 知道业务 schema，"模型返回了不合规 JSON" 与 "网络失败" 就会混在同一层处理，而这两者的正确应对完全不同（前者不该重试，后者该重试）。分层后，`StructuredOutputParser` 是独立的、可单测的纯函数。

调用链：

```
Prompt 构造 → Provider.complete() → 原始文本 + usage
                                   → StructuredOutputParser.parse() → 校验过的 Pydantic 模型
```

### 2. 失败是类型，不是返回值

失败一律用异常表达，且每个异常绑定一个 `failure_class`（取值来自 `contracts/events/README.md` 的封闭枚举）。

```
ProviderError (基类，携带 failure_class / retriable / attempt)
├── ProviderTimeout            → provider_failure   retriable
├── ProviderRateLimited        → provider_failure   retriable（带 retry_after）
├── ProviderUnavailable        → provider_failure   retriable（5xx）
├── ProviderAuthError          → authorization_failed  NOT retriable
├── ProviderBadResponse        → provider_failure   NOT retriable（包络结构不对/空/截断）
├── ProviderBudgetExceeded     → cost_budget_exhausted NOT retriable
└── ProviderContentFiltered    → safety_rule_triggered NOT retriable
```

结构化输出的失败是另一族，因为它不属于 Provider 层：

```
StructuredOutputError (基类)
├── MalformedJsonError    → provider_failure  NOT retriable
└── SchemaViolationError  → provider_failure  NOT retriable
```

**为什么用异常而不是 `Result` 类型**：返回值可以被忽略。异常迫使调用方处理，或者让失败向上冒泡成明确的终态——不会出现"忘记检查返回值所以当成功了"。

**为什么鉴权失败不可重试**：401/403 重试只会浪费预算并可能触发服务商的风控。它是配置问题，不是瞬时故障。

### 3. 不做静默修补，但允许**记录在案的**包络解包

DEV_PROMPT 要求"模型返回不合法 JSON 时走 typed failure，不许静默修补后当成功"。

严格执行会遇到一个现实问题：许多模型在被要求输出 JSON 时会包一层 markdown 代码围栏（```json ... ```）。这是**传输层的包装**，不是内容错误。

裁决：**允许剥离代码围栏，但必须在结果里标记 `unwrapped=true`。** 关键词是"不许**静默**修补"——被记录、可断言、会出现在 Trace 里的解包不是静默修补。

明确不做的修补：

- 不补全缺失的引号或括号
- 不猜测截断的 JSON 该如何结束
- 不把单引号换成双引号
- 不删除尾随逗号
- 不从一段散文里"找出看起来像 JSON 的部分"（只接受整个文本或整个代码围栏内容是 JSON）
- schema 不匹配时不填默认值

以上任一情形一律 `MalformedJsonError` / `SchemaViolationError`。

### 4. 重试：有界、仅对可重试错误、确定性退避

- 只有 `retriable=True` 的错误才重试。
- 默认上限 3 次尝试（含首次）。
- 退避 0.5s / 2s，指数增长，**无随机抖动**——测试需要可预测的时长。生产环境的抖动需求由调用方在上层加，不在 Provider 内部。
- `ProviderRateLimited` 若带 `retry_after`，优先采用它。
- 重试耗尽后抛出最后一次的异常，`attempt` 字段记录总尝试次数。

**为什么无抖动**：抖动是为了避免惊群，而本项目单 Agent、低并发，惊群不是现实问题；但不确定的退避时长会让"429 退避后成功"这类测试变得不稳定。

### 5. 硬调用上限，保护预算

真实 Provider 强制两道上限：

- `RUNBOOKGUARD_LLM_MAX_CALLS`（默认 20）：进程生命周期内的总调用次数。超过抛 `ProviderBudgetExceeded`。
- `RUNBOOKGUARD_LLM_MAX_COST_MICROS`（默认 0 = 不限）：累计成本上限。

**为什么放在 Provider 层而不是只在 Run 层**：Run 层的预算是业务语义（这个 Run 允许花多少），Provider 层的上限是防呆闸（整个进程最多花多少）。M3 的"一次受控真实验证"依赖后者——一个循环 bug 不该把预算烧光。这两层都要有。

### 6. 凭据只从环境变量读，不落任何文件

- `RUNBOOKGUARD_LLM_API_KEY`：只从 `os.environ` 读取。
- 不支持从配置文件、命令行参数、请求体读取 key。
- key 不出现在日志、异常消息、Trace、响应体中。`ProviderConfig.__repr__` 遮盖它。
- 所有出站日志过 `redaction.redact()`（复用 M1 的脱敏思路，Python 侧独立实现）。

### 7. fake provider 必须能模拟每一种失败

这是本 ADR 最关键的一条。fake provider 支持脚本化行为：

```python
FakeProvider(script=[
    FakeTurn(text='{"root_cause": "..."}'),        # 正常
    FakeTurn(raise_=ProviderTimeout()),             # 超时
    FakeTurn(text="not json at all"),               # 垃圾
    FakeTurn(text='{"root_cause":'),                # 截断
    FakeTurn(text="```json\n{...}\n```"),           # 带围栏
    FakeTurn(raise_=ProviderRateLimited(retry_after=0.01)),
])
```

**因此基于 fake 的失败测试是真实的测试**：它们测的是 Adapter 与 Parser 对异常输入的处理，而异常输入本身由测试指定，不是由被测代码决定。

fake 的成功响应也必须**确定性**（同一 prompt → 同一输出，blake2b 派生），否则 CI 不可复现。

同时有一条**契约测试**：fake 与真实 Adapter 必须实现同一个 Protocol，且返回同构的 `Completion`。这条测试防止 fake 逐渐漂移成一个真实 Provider 无法满足的理想化接口。

### 8. SSE 流式：未完成的流不算成功

- 流式接口逐块产出 `StreamChunk`，最后产出一个 `Completion`。
- 流在收到终止标记（`data: [DONE]`）前断开 → 抛 `ProviderBadResponse`，**已消费的分块不构成结果**。
- 流式模式下不做结构化输出解析：解析需要完整文本，因此结构化输出走非流式。流式只用于给控制台做进度展示。

**为什么**：如果允许"流断了但已经收到的部分算结果"，那么一次网络抖动就会产出一个被截断的诊断结论，而它看起来是成功的。这正是 Gate 条件要防的那种假成功。

## 待核验项（铁律五）

以下三项已在 2026-08-27 完成实测，结论见下一节「已核验项」。本节保留原始待办以显示核验前的状态：

| 项 | 核验前状态 |
|---|---|
| 目标网关 `https://opencode.ai/zen/go/v1` 的实际行为 | 未核验（第三方网关而非模型厂商官方域名） |
| 模型 `glm-5.3-flash` 的存在性与能力 | 未核验 |
| `response_format: {"type":"json_object"}` 是否被支持 | 未核验，实现为可开关，默认关闭 |

## 已核验项（2026-08-27 实际调用结果）

铁律五要求版本敏感的东西当场核验。以下是对目标网关的实测结论，**来自真实调用**，
artifact 见 `eval/reports/m3-real-provider/run-20260827T062409Z.json`。

| 项 | 结论 | 依据 |
|---|---|---|
| 网关 `https://opencode.ai/zen/go/v1` 遵循 Chat Completions 协议 | **是** | `POST /chat/completions` 返回标准包络（choices / message.content / finish_reason / usage） |
| 模型 `glm-5.3-flash` 可用 | **是** | 单次诊断请求返回 `finish_reason=stop`，usage 1314 tokens（prompt 600 / completion 714） |
| `response_format: {"type":"json_object"}` 被支持 | **是** | 探测请求返回 `{"status":"ok"}`，无 400 |
| SSE 流式可用且带终止标记 | **是** | 流式请求收到 2 个分块 + `data: [DONE]` |
| 401 的错误体形状 | 非 OpenAI 标准 | 返回 `{"type":"error","error":{"type":"AuthError","message":"Invalid API key."}}`，而非 OpenAI 的 `{"error":{"code":...}}`。Adapter 按 HTTP 状态码而非错误体结构分类，因此不受影响 |
| 无效凭据不重试 | **是** | `ProviderAuthError`，`retriable=False`，`calls_made=1`（`max_attempts=3` 下仍只调一次） |
| 错误消息不泄漏 key | **是** | 网关的 401 响应体不回显被拒的 key；脱敏层也未被触发 |

**未核验**：并发限流阈值、实际计价（`price_per_1k_*_micros` 仍为 0，因此成本记为 0 并在报告中标注未知）、长时间稳定性。

**该模型在本次调用中未把 JSON 包进 markdown 围栏**（`unwrapped=False`），因此 §3 的解包路径在真实调用中尚未被触发——它只在 fake 测试中被验证过。

## Consequences


**正面**：

- 失败是类型化的，且与 `failureClass` 枚举一一对应，M6 的失败归因统计有据可依。
- fake 能模拟全部失败模式，CI 无成本且确定性，同时测试是真实的。
- 双层预算闸，一个循环 bug 烧不掉整个预算。
- 结构化输出解析与 Provider 解耦，各自可单测。

**负面 / 代价**：

- 允许剥离代码围栏是对"绝不修补"的一次让步。缓解是强制标记 `unwrapped=true`，使其可被断言与审计，但**这确实是一处需要在评审时说明的偏离**。
- 流式不支持结构化输出，控制台的流式展示与最终结论是两次不同的调用。
- 退避无抖动，高并发场景会惊群。当前规模下不是问题，扩容前须重新评估。
- Provider 层的硬调用上限是进程级的，多进程部署时上限会被放大到"进程数 × 上限"。M8 部署时需要注意。

## 验证方式

对应 M2.5 Gate 报告 §6.3 列出的 8 条失败测试，逐条落到测试文件：

1. 不合法 JSON → `MalformedJsonError`，不返回任何"部分成功"
2. 合法 JSON 但违反 schema → `SchemaViolationError`
3. 超时 → `ProviderTimeout`，重试有界
4. 429 → 按 `retry_after` 退避，超上限后抛出
5. 空响应 / 截断响应 → `ProviderBadResponse`
6. SSE 中途断开 → `ProviderBadResponse`，已消费分块不成为结果
7. 响应含疑似凭据 → 出口脱敏
8. fake 与真实 Adapter 的 Protocol 契约一致

外加：

9. 鉴权失败不重试
10. 调用次数超上限 → `ProviderBudgetExceeded`
11. 代码围栏被剥离时 `unwrapped=true`
12. fake 的成功响应对同一 prompt 确定性
