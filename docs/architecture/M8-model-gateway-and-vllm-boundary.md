# 模型网关与 vLLM 的职责边界

> 状态：**架构理解与客户端契约**，不含任何性能验证
> 日期：2026-08-30
> 上游依据：DEV_PROMPT §12 M8 要点、说明书 §21

---

## 0. 这份文档不包含什么

**没有真实 GPU，因此这里没有任何推理性能数字。** DEV_PROMPT §12 M8 原文：

> 没有真实 GPU 时，vLLM 部分只能写架构理解、客户端契约和 mock gateway，
> **不能写成真实推理性能验证**。

具体地：吞吐、首 token 延迟、并发上限、KV cache 命中率、
continuous batching 的实际收益——这些**一个都没有测过**。
下文出现的任何量级描述都标注了来源是公开文档或推导，不是本项目的测量。

已经测过的是**客户端侧**的东西：Provider Adapter 对 OpenAI-compatible 接口的
行为（M3，含 4 次真实网关调用）、失败分类、成本记账。那一层与后端是 vLLM
还是别的什么无关，这也正是设 Provider 边界的收益。

---

## 1. 三层职责，谁不该知道谁的事

```
┌──────────────────────────────────────────────────────────┐
│ Agent Runtime（本项目）                                   │
│   知道：ChatMessage / Completion / typed failure / 预算    │
│   不知道：模型权重、batching、KV cache、GPU 拓扑           │
└───────────────────────┬──────────────────────────────────┘
                        │ OpenAI-compatible HTTP
                        │ POST /v1/chat/completions
┌───────────────────────▼──────────────────────────────────┐
│ 模型网关（本项目不实现，只定契约）                          │
│   知道：路由、配额、多租户计费、模型别名、鉴权、限流         │
│   不知道：Agent 的业务语义（Run / Approval / Incident）    │
└───────────────────────┬──────────────────────────────────┘
                        │ 同样是 OpenAI-compatible
┌───────────────────────▼──────────────────────────────────┐
│ 推理引擎（vLLM / TGI / SGLang / 云厂商 API）               │
│   知道：权重加载、PagedAttention、continuous batching、    │
│         tensor parallel、量化                            │
│   不知道：调用方是谁、配额、计费                            │
└──────────────────────────────────────────────────────────┘
```

**三条「不该知道」是这个边界的全部意义。** 逐条说为什么：

### Agent Runtime 不该知道 batching

如果 Agent Runtime 里出现「凑够 8 条请求再发」这样的逻辑，
它就把推理引擎的调度职责搬到了业务侧。后果是具体的：
换一个已经做了 continuous batching 的后端时，两层攒批会互相打架，
延迟反而上升，而排查时没人会想到问题在业务代码里。

本项目的实现里 `OpenAICompatibleProvider` 一次 `complete()` 就是一次 HTTP 请求，
没有任何攒批。并发由调用方（评测 harness、Worker）决定。

### 网关不该知道 Run / Approval

网关看到的是「某个 principal 用某个模型消耗了多少 token」。
它不知道这次调用属于哪个 Run，也不知道 Run 有没有审批挂起。
理由：网关是可以被多个产品共用的基础设施，让它理解一个产品的领域模型
会让那个产品变成它的依赖。

代价是**预算必须在两处记账**：网关侧记「这个 key 花了多少」（防超支），
Agent Runtime 侧记「这个 Run 花了多少」（有界执行）。
两者的数字不会完全一致（重试、缓存命中、取整），因此
`AgentRun.cost_spent_micros` 是本项目的权威值，网关的数字用于对账而非判定。

### 推理引擎不该知道配额

vLLM 没有多租户配额概念（截至公开文档所述的 v0.x/v1 系列都是如此）。
把配额放进引擎意味着改引擎代码，而引擎升级频繁。
配额属于网关。

---

## 2. 客户端契约（这一层本项目真的实现并测过）

`apps/agent-runtime-python/src/agent_runtime/provider/openai_compatible.py`
对后端的假设**只有**以下几条。任何满足它们的后端都可以替换，
包括 vLLM 的 OpenAI-compatible server。

### 2.1 请求

```
POST {base_url}/v1/chat/completions
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "model": "<string>",
  "messages": [{"role": "system|user|assistant", "content": "<string>"}],
  "stream": false,
  "temperature": <float, 可选>,
  "max_tokens": <int, 可选>
}
```

**不使用的特性，以及为什么：**

| 特性 | 不用的理由 |
|---|---|
| `response_format: {"type": "json_object"}` | 网关支持情况不一（ADR-0005 待核验项）。改为在 prompt 里给 schema + 服务端 Pydantic 校验，失败走 typed failure。这样后端支持与否都能工作 |
| `tools` / `function_calling` | 模型的工具建议由本项目自己解析。用后端的 tool calling 会把「建议」和「调用」的边界交给后端，而三段分离要求那条边界在我这里 |
| `logprobs` | 用不到 |
| `n > 1` | 多候选会让「跑一次」的评测口径失效 |
| `seed` | 后端支持不一致，且它给出的确定性无法跨版本保证。本项目的确定性靠 fake provider |

### 2.2 响应（非流式）

必需字段：

```json
{
  "choices": [{"message": {"content": "<string>"}, "finish_reason": "stop|length|..."}],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
}
```

`usage` 缺失时本项目**记为 0 并标注未知**，不猜测——猜出来的 token 数会让
预算判定失效，而失效的方式是静默的。

### 2.3 流式

`text/event-stream`，`data: {json}\n\n`，以 `data: [DONE]\n\n` 结束。

**没有收到 `[DONE]` 的流不算完整结果**（M3 判据）。这一点对 vLLM 同样成立：
连接中断时已经吐出的分块不构成一个诊断结论。

### 2.4 失败与状态码的映射（已测）

| 状态 / 现象 | 映射 | 可重试 | failure_class |
|---|---|---|---|
| 连接超时 / 读超时 | `ProviderTimeout` | 是 | `provider_failure` |
| 429 | `ProviderRateLimited` | 是 | `provider_failure` |
| 500 / 502 / 503 / 504 | `ProviderUnavailable` | 是 | `provider_failure` |
| 401 / 403 | `ProviderAuthError` | **否** | `authorization_failed` |
| 200 但 JSON 无法解析 | `ProviderBadResponse` | 否 | `provider_failure` |
| 200 但内容不符合 schema | `SchemaViolationError` | 否 | `provider_failure` |
| 内容被安全策略拦 | `ProviderContentFiltered` | 否 | `safety_rule_triggered` |
| 本进程调用/成本超限 | `ProviderBudgetExceeded` | 否 | `cost_budget_exhausted` |

401 不可重试这一条是刻意的：重试只会烧完配额并延迟发现配置错误（ADR-0005）。

**一个已核验的现实**：本项目用的网关在 401 时返回的**不是** OpenAI 形状，
而是 `{"type":"error","error":{"type":"AuthError",...}}`。
Adapter 因此不依赖错误体的形状，只依赖状态码。
换成 vLLM 时错误体形状又会不同——这正是「只依赖状态码」的收益。

---

## 3. 如果要接 vLLM，需要改什么

**Agent Runtime 侧：改 3 个环境变量，不改代码。**

```bash
RUNBOOKGUARD_LLM_PROVIDER=openai-compatible
RUNBOOKGUARD_LLM_BASE_URL=http://vllm-gateway.runbookguard:8000
RUNBOOKGUARD_LLM_MODEL=<vLLM --served-model-name 的值>
RUNBOOKGUARD_LLM_API_KEY=<网关签发的 key>
```

**K8s 侧：需要一条新的 NetworkPolicy egress 规则。**

当前 `agent-runtime-policy` 的 egress 白名单里只有 control-plane 与 synthetic-lab，
因此它**连不上任何模型服务**（M8 演练实测：访问 1.1.1.1 被拒）。
接 vLLM 必须显式加一条：

```yaml
  egress:
    - to:
        - podSelector:
            matchLabels:
              app: vllm-gateway
      ports:
        - protocol: TCP
          port: 8000
```

这个「必须显式加」是设计意图而不是麻烦：一条新的出站路径应当是一次
被审查的动作，而不是默认就通。

**新增两项必须重新核验的东西：**

1. `usage` 字段的存在性与准确性。vLLM 的 `usage` 在流式响应里的行为
   与非流式不同（公开文档描述其需要 `stream_options.include_usage`），
   不核验会让成本记账静默归零。
2. `pricing.py` 里的计价。自托管 vLLM 没有 per-token 价格，
   成本应当按 GPU 时长摊算——那是一个不同的计价模型，
   现在的 `lookup()` 对未知模型返回 `None` 并把成本记为 0，
   会让 cost budget 这条终止条件失效。**这是一个真实的缺口**，
   接自托管后端前必须先解决。

---

## 4. vLLM 的关键机制（架构理解，非本项目测量）

以下是为了能在面试现场回答「你知道 vLLM 在做什么吗」而整理的理解，
来源是公开论文与文档，**本项目没有验证过任何一条**。

### PagedAttention

传统实现给每个序列预分配连续的 KV cache（按 max_seq_len），
实际长度远小于上限时大部分显存被浪费。
PagedAttention 借用操作系统分页的思路：KV cache 切成固定大小的 block，
用 block table 做逻辑到物理的映射，因此不需要连续显存，
也使多个序列共享相同前缀的 block 成为可能（prefix caching）。

对本项目的意义：诊断 prompt 里的 system 段是固定的，
如果后端支持 prefix caching，那一段的 KV 可以跨请求复用。
**但这不是我能在客户端控制或验证的**，也不该为它改 prompt 结构——
为了迁就某个后端的缓存策略而调整 prompt，会让 prompt 指纹（M6 冻结清单的一项）
依赖后端实现。

### Continuous batching

不等一个 batch 全部完成再收下一批，而是在每个解码步后把已完成的序列换出、
把等待中的换入。收益在于长短请求混合时 GPU 不会被最长的那个请求拖住。

对本项目的意义：**Agent Runtime 不该做任何攒批**（§1）。
并发度由评测 harness 与 Worker 数量决定，引擎自己去调度。

### Tensor / pipeline parallel

模型放不进单卡时按张量维度或层维度切分。
对客户端完全透明——这也是为什么 Provider Adapter 里没有任何相关代码。

---

## 5. mock gateway（本项目提供的部分）

`FakeProvider`（`provider/fake.py`）扮演的就是这个角色，且刻意能模拟**每一种失败**：

| 场景 | 构造方式 |
|---|---|
| 正常响应 | `FakeProvider()`，默认响应满足 `Diagnosis` schema |
| 返回散文而非 JSON | `FakeTurn(text="The database is probably slow.")` |
| 合法 JSON 但缺字段 | `FakeTurn(text='{"root_cause":"x"}')` |
| 超时 / 429 / 401 | `FakeTurn(raise_=ProviderTimeout())` 等 |
| 流式中途断开 | `FakeTurn(break_stream=True)`，不发 `[DONE]` |
| 调用预算耗尽 | `max_calls=N` |

**fake 只会返回完美响应时，基于它的全绿不构成任何证明**——
Adapter 必然能处理完美响应。这是 M3 就确立的原则。

M6 的真实模型测量又暴露了 fake 的另一个局限：它只需要 prompt 里出现
evidence_id 就能构造合规输出，因此完全掩盖了「prompt 里没有给出观测值」
这个产品缺陷（M6 Gate 报告 §7.2b）。
**任何模型侧替身都不能作为唯一验证手段**，这是比「fake 要能模拟失败」更强的结论。

---

## 6. 本项目在这个边界上的已知缺口

| # | 缺口 | 后果 |
|---|---|---|
| 1 | 自托管后端的计价模型未实现 | `lookup()` 返回 None → 成本记 0 → cost budget 终止条件失效 |
| 2 | 流式 `usage` 未核验 | 成本可能静默归零 |
| 3 | 无真实 GPU，全部性能描述均为公开资料 | 不能声称做过性能验证 |
| 4 | 未验证过 vLLM 的实际 401/429 响应形状 | Adapter 只依赖状态码，风险低但未测 |
| 5 | 网关侧配额与 Run 侧预算的对账未实现 | 两处数字不一致时无法定位 |

第 1 条是其中最需要先解决的：它会让一个安全机制（有界执行）在换后端后静默失效。
