# M3 Gate 自检报告

- 里程碑：**M3 — 模型 Runtime**
- 报告日期：2026-08-27
- 结论：**M3 Gate 通过（有真实输出，含一次真实模型调用）。**
- 上游依据：说明书 §23 M3，DEV_PROMPT §12 M3

---

## 1. Gate 条件逐条自评

DEV_PROMPT §12 M3 Gate 原文：**「模型失败（超时、返回垃圾、限流）不能产生假成功，每种失败都有对应测试。」**

| # | 失败模式 | 对应测试 | 表现 | 自评 |
|---|---|---|---|---|
| 1 | 超时 | `TestTimeout` 3 项 | `ProviderTimeout`，retriable，重试有界（`max_attempts=3` 时恰好调 3 次） | **通过** |
| 2 | 限流 429 | `TestRateLimit` 3 项 | `ProviderRateLimited`，按 `retry-after` 退避 | **通过** |
| 3 | 返回垃圾（非 JSON） | `TestMalformedJson` 7 项 | `MalformedJsonError`，不修补 | **通过** |
| 4 | 合法 JSON 但违反 schema | `TestSchemaViolation` 4 项 | `SchemaViolationError`，不填默认值 | **通过** |
| 5 | 空响应 / 截断 / 包络异常 | `TestBadResponse` 6 项 | `ProviderBadResponse`；`finish_reason=length` 也判失败 | **通过** |
| 6 | 鉴权失败 | `TestAuthAndNonRetriable` 4 项 | `ProviderAuthError`，**不重试**（真实调用实测 `calls_made=1`） | **通过** |
| 7 | 内容审核拒答 | `test_content_filter_is_typed_separately` | `ProviderContentFiltered` → `safety_rule_triggered` | **通过** |
| 8 | SSE 流中途断开 | `TestStreaming` 3 项 | `ProviderBadResponse`，已消费分块不成为结果，**不发 `[DONE]`** | **通过** |
| 9 | 预算耗尽 | `TestBudgetGuard` 3 项 | `ProviderBudgetExceeded`；重试计入调用上限 | **通过** |
| 10 | Secret 泄漏 | `TestSecretRedaction` 4 项 | `repr` 遮盖 key；错误体过脱敏 | **通过** |

| # | 其它 Gate 相关条件 | 自评 | 证据 |
|---|---|---|---|
| G11 | fake provider，CI 默认用它 | **通过** | `build_provider()` 默认返回 fake；容器 `ENV RUNBOOKGUARD_LLM_PROVIDER=fake`；镜像启动不产生任何真实调用 |
| G12 | 结构化输出必须 Pydantic 校验 | **通过** | `parse_structured` + `strict=True`，类型不做隐式转换 |
| G13 | 不许静默修补后当成功 | **通过（有一处记录在案的偏离）** | 见 §5.1 第 1 条：允许剥离 markdown 围栏，但强制标记 `unwrapped=true` 并暴露给调用方 |
| G14 | SSE 流式 | **通过** | 15 个 delta + final + `[DONE]`（§3.3） |
| G15 | 一次受控的真实模型合成数据验证 | **通过** | §3.4，4 次真实调用，artifact 落盘 |

---

## 2. 交付物清单

### 2.1 决策

| 文件 | 内容 |
|---|---|
| [docs/adr/ADR-0005-provider-boundary.md](../adr/ADR-0005-provider-boundary.md) | Provider 职责边界、失败类型体系、重试策略、双层预算闸、fake 必须能模拟每种失败、SSE 语义。含对目标网关的**实测核验结论** |

### 2.2 源码（`apps/agent-runtime-python/`）

| 文件 | 职责 |
|---|---|
| `provider/errors.py` | 类型化失败，每个绑定一个 `failure_class`（取值来自事件契约的封闭枚举） |
| `provider/models.py` | `ChatMessage` / `Usage` / `Completion` / `StreamItem` / `ProviderConfig`。`__repr__` 遮盖 api_key |
| `provider/base.py` | `ChatProvider` Protocol。用 Protocol 而非 ABC：fake 的价值在于不共享真实实现的代码路径 |
| `provider/openai_compatible.py` | 真实 Adapter。重试、退避、包络解析、SSE、状态码分类 |
| `provider/fake.py` | 可脚本化的 fake，能模拟每种失败；成功响应由 blake2b 派生（确定性） |
| `provider/structured.py` | 结构化输出解析。围栏剥离标记 `unwrapped` |
| `provider/factory.py` | 装配。**fake 是默认**；请求真实 Provider 但缺配置时报错，不静默回退 |
| `schemas.py` | 规范 `Diagnosis` schema 的单一定义点 |
| `redaction.py` | Python 侧脱敏，与 Java 侧同规则 |
| `app.py` | FastAPI：`/health`、`/v1/complete`、`/v1/diagnose`、`/v1/complete/stream` |
| `Dockerfile` | 非 root，默认 fake provider |

### 2.3 测试与脚本

| 文件 | 内容 |
|---|---|
| `tests/test_provider.py` | 57 项（Provider 层 + 结构化输出 + fake + 契约） |
| `tests/test_app.py` | 18 项（HTTP 失败映射） |
| `scripts/smoke-m3.sh` | 14 项真实 HTTP |
| `scripts/validate-m3-real-provider.py` | 受控真实模型验证，artifact 落盘 |
| `eval/reports/m3-real-provider/run-*.json` | 真实调用记录 |

---

## 3. 真实运行输出

### 3.1 单元与接口测试（fake provider，零网络）

```
$ cd apps/agent-runtime-python && PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
........................................................................ [ 96%]
...                                                                      [100%]
75 passed in 0.81s
```

### 3.2 容器冒烟（打包镜像 + 真实 HTTP）

```
$ bash scripts/smoke-m3.sh

== 前置检查 ==
ok    健康检查                                    [200]
ok    默认 provider 是 fake（不产生真实调用）      [fake]

== 成功路径 ==
ok    非流式补全 -> 200                           [200]
ok    结构化诊断 -> 200                           [200]
ok    解包标记对调用方可见                        [False]

== 输入校验 ==
ok    空 messages -> 422                          [422]
ok    缺 messages 字段 -> 422                     [422]
ok    messages 类型错误 -> 422                    [422]

== SSE 流式 ==
ok    流式产出 15 个 delta
ok    流式以 final + [DONE] 收尾（完整结果的标志）

== 契约：响应字段齐全 ==
ok    /v1/complete 返回全部契约字段
ok    usage 含三个 token 计数

== Secret 不泄漏 ==
ok    响应未回显疑似凭据

== 未配置真实 Provider 时不静默回退 ==
ok    请求真实 Provider 但缺配置时报错，不回退到 fake

===============================
PASS=14  FAIL=0
```

### 3.3 容器健康

```
$ docker ps --format '{{.Names}} {{.Status}}' | grep -E 'agent-runtime|synthetic'
rg-agent-runtime Up 20 seconds (healthy)
rg-synthetic-lab Up 4 hours (healthy)

$ curl -s http://127.0.0.1:8100/health
{"status":"ok","provider_id":"fake","calls_made":0}
```

### 3.4 受控真实模型验证

**四个限定词的落实情况：**

| 限定词 | 落实 |
|---|---|
| 真实模型 | 目标网关 `https://opencode.ai/zen/go/v1`，模型 `glm-5.3-flash`。真实 HTTP，真实 token 计费 |
| 合成数据 | 输入全部来自 synthetic-lab 的 `db-pool-exhaustion-v1`（fingerprint `ccc3bcbda01f7dd7...`），**无任何真实生产日志** |
| 受控 | `max_calls=8` 硬上限；逐轮记录；artifact 落盘；凭据只从环境变量读，不写入任何文件；**不进 CI** |
| 一次 | 手动执行，共 4 次真实调用 |

```
$ RUNBOOKGUARD_LLM_BASE_URL=... RUNBOOKGUARD_LLM_MODEL=... RUNBOOKGUARD_LLM_API_KEY=... \
  apps/agent-runtime-python/.venv/bin/python scripts/validate-m3-real-provider.py

config: ProviderConfig(base_url='https://opencode.ai/zen/go/v1', model='glm-5.3-flash',
        api_key=***, timeout_seconds=60.0, max_attempts=2, max_calls=8, json_mode=False)

synthetic evidence from scenario db-pool-exhaustion-v1
  fingerprint ccc3bcbda01f7dd7...
  4 metric summaries, 4 log samples, 1 deployments

[1/4] non-streaming structured output
      provider ok: finish=stop tokens=1398 attempts=1
      schema valid (unwrapped=False)
      root_cause: Database connection pool exhaustion on pool=orders-primary. Following the
                  v1.5.0 deploy, whose only changed config key w...

[2/4] streaming (SSE)
      3 chunks, terminator seen: True

[3/4] wrong credentials must be a non-retriable auth failure
      ProviderAuthError failure_class=authorization_failed retriable=False calls=1

[4/4] response_format json_object support
      supported: '{"status":"ok"}'

artifact written: eval/reports/m3-real-provider/run-20260827T063620Z.json
total real calls: 4
```

**注意 `api_key=***`**：配置对象的 `repr` 在这条输出里就遮盖了凭据。这是 §2.2 中 `ProviderConfig.__repr__` 的实际效果，不是事后编辑。

**实测结论（回填到 ADR-0005 的「已核验项」）：**

| 项 | 结论 |
|---|---|
| 网关遵循 Chat Completions 协议 | 是。标准包络（choices / message.content / finish_reason / usage） |
| `glm-5.3-flash` 可用 | 是。1398 tokens（prompt 600 / completion 798），`finish_reason=stop` |
| `response_format: json_object` 被支持 | 是。返回 `{"status":"ok"}`，无 400 |
| SSE 带终止标记 | 是。3 个分块 + `data: [DONE]` |
| 401 错误体形状 | **非 OpenAI 标准**：`{"type":"error","error":{"type":"AuthError","message":"Invalid API key."}}`。Adapter 按 HTTP 状态码而非错误体结构分类，因此不受影响 |
| 无效凭据不重试 | 是。`max_attempts=3` 下仍只调 1 次 |
| 错误消息不泄漏 key | 是。网关的 401 响应体不回显被拒的 key |

**真实模型在两次运行中都直接返回了裸 JSON**（`unwrapped=False`），因此围栏剥离路径**在真实调用中尚未被触发**，它只被 fake 测试覆盖过。这一点如实记录。

---

## 4. 开发过程中真实踩到的问题

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | **单测 75 项全绿，容器冒烟 `/v1/diagnose` 返回 502** | `app.py` 与 `test_provider.py` **各自定义了一份 `Diagnosis`**，字段名漂移（`evidence` vs `evidence_ids`）。两边单测各用各的定义所以都通过，fake 的默认响应只满足其中一个 | 抽出 `schemas.py` 作为单一定义点；新增两条测试把规范 schema 钉在 fake 的默认响应上 |
| 2 | 工厂在缺配置时抛 `ValueError` 而非 `ProviderConfigurationError` | `ProviderConfig.__post_init__` 先于缺失检查执行，那个错误说不出「缺哪几个变量」 | 先查环境变量再构造配置 |

第 1 条是本里程碑最值得记的。它是**测试与实现共享同一个错误假设**的典型：两处定义各自内部一致，所以任何单测都发现不了。抓到它的唯一原因是 Gate 要求容器化后重跑冒烟——如果只看 `pytest` 的 75 passed 就宣布通过，这个 bug 会一路带到 M4，然后表现为「Agent 输出的诊断结构与消费方期待的不一致」。

已加的防线：`DIAGNOSIS_FIELDS` 常量 + 断言 fake 默认响应的字段集与规范 schema **完全相等**（不是子集）。改 schema 时这条会立刻失败。

---

## 5. 已知限制与未验证项

### 5.1 已知限制（设计决策）

1. **允许剥离 markdown 代码围栏**，这是对 DEV_PROMPT「不许静默修补」的一处偏离。缓解：强制标记 `unwrapped=true` 并在 HTTP 响应里暴露给调用方，使它可断言、可审计、会进 Trace。关键词是「不许**静默**修补」——被记录的解包不是静默修补。**但这确实需要在评审时说明。**
2. **退避无随机抖动**。高并发会惊群；当前单 Agent 低并发不是问题，扩容前须重新评估。
3. **Provider 层的调用上限是进程级的**。多进程部署时实际上限是「进程数 × 上限」。M8 部署时需注意。
4. **计价未配置**（`price_per_1k_*_micros=0`），因此 `cost_micros` 恒为 0。真实费用**未知**，不是零。M6 前需要配置真实价格，否则成本预算形同虚设。
5. **流式不支持结构化输出**。解析需要完整文本，因此结构化走非流式；流式只用于控制台进度展示。
6. **流式不重试**。已经吐出去的分块无法撤回，重试会让消费者看到重复内容。
7. **真实模型的围栏输出路径未被真实触发**（§3.4 末）。

### 5.2 未验证项

| 项 | 状态 |
|---|---|
| 目标网关的并发限流阈值 | **未验证**。未做并发压测 |
| 真实计价 | **未知**。价格参数为 0 |
| 长时间稳定性、连接池行为 | **未验证** |
| 真实模型返回围栏 JSON 时的解包 | **未验证**（真实调用中未出现该情形） |
| 真实模型触发内容审核时的响应形状 | **未验证**。`ProviderContentFiltered` 只被 fake 覆盖 |
| 真实超时 / 真实 429 的实际行为 | **未验证**。这两条只被 respx mock 与 fake 覆盖 |
| Java Control Plane 与 Agent Runtime 的联通 | **未做**，M4 |
| M0 五个正式阈值 | **未验证** |
| S0 学习计划状态 | `UNKNOWN`（M0 Gate 报告 Q1，仍未回答） |

---

## 6. 下一里程碑（M4）的前置依赖

M4 交付：LangGraph 图与状态、5 个只读工具、1 个只读 MCP Server + direct adapter、Policy 引擎、Approval 等待与恢复、Checkpoint 持久化。

### 6.1 阻塞项

| # | 前置依赖 | 状态 |
|---|---|---|
| B1 | M3 Gate 验收 | 待验收 |
| B2 | **LangGraph 当日 API 核验**（node/edge/state/checkpointer/interrupt 接口形状与持久化后端） | `UNKNOWN`，铁律五要求动手前核验并写 ADR-0006 |
| B3 | **MCP 当日正式协议版本与 SDK 支持情况** | `UNKNOWN`，核验后写 ADR-0007 |
| B4 | 动作工具沙箱实现方式（独立进程 vs 容器） | `UNKNOWN`，M4 决策 |
| B5 | 是否新增第 6 个只读工具 `get_runtime_events` | `UNKNOWN`。若新增，属于对说明书 §14 五工具清单的偏离，需新 ADR |

### 6.2 M4 必须承接的历史遗留项

1. **只读工具层剥离 `is_decoy`**（M2.5 Gate 报告 §5.1 第 5 条）。当前 synthetic-lab 直接返回该字段，Agent 能读出「哪次部署是诱饵」，S2 的误归因检验会失效。我在 M3 的验证脚本里已经手工剔除了它，但工具层必须做成强制的。
2. **跨语言摘要测试向量**（M1 Gate 报告 §5.2）。`contracts/tools/digest-test-vectors.json` 尚未生成；Python 侧实现 `arguments_digest` 时必须与 Java 侧用同一份向量交叉校验，否则审批摘要在两侧算出不同值，T-2 的缓解直接失效。
3. **交叉校验工具契约与 synthetic-lab 契约**：字段名、类型、时间格式两侧一致。
4. **配置真实计价参数**（§5.1 第 4 条），否则 cost budget 形同虚设。

### 6.3 M4 应先写的失败测试

按铁律二，八条终止条件 + 安全红线：

1. max_steps 用尽 → 唯一终态 `FAILED/max_steps_exhausted`
2. deadline 到期 → `FAILED/deadline_exceeded`
3. token / cost budget 超限 → 对应 failureClass
4. tool-call budget 超限 → `FAILED/tool_call_budget_exhausted`
5. repeated-state 命中 → `FAILED/repeated_state_detected`
6. 权限校验失败 → `FAILED/authorization_failed`
7. graph / checkpoint 版本不兼容 → `FAILED/version_incompatible`，**不硬恢复**
8. 安全规则命中 → `BLOCKED` 或 `FAILED/safety_rule_triggered`
9. **未审批的写动作 100% 不执行**（M4 Gate 硬条件）
10. 审批后篡改参数被 digest 拦住（跨语言）
11. `AWAITING_APPROVAL` 跨进程重启后可恢复
12. 注入日志诱导高危工具 → Policy 拒绝 + 审计留痕

---

## 7. 可复现命令

```bash
cd /Users/lijunpeng/Desktop/open_source_project/RunbookGuard-Agent-Platform/apps/agent-runtime-python

# 首次准备
python3 -m venv .venv
.venv/bin/pip install 'fastapi==0.115.6' 'uvicorn[standard]==0.34.0' 'pydantic==2.10.4' \
  'httpx==0.28.1' 'pytest==8.3.4' 'pytest-asyncio==0.25.2' 'respx==0.22.0'

# 测试（零网络、零成本）
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q

# 容器
cd ../../deploy/compose && docker compose up -d --build agent-runtime synthetic-lab

# 冒烟
cd ../.. && bash scripts/smoke-m3.sh

# 受控真实模型验证（需要凭据；只从环境变量读）
export RUNBOOKGUARD_LLM_BASE_URL='https://opencode.ai/zen/go/v1'
export RUNBOOKGUARD_LLM_MODEL='glm-5.3-flash'
export RUNBOOKGUARD_LLM_API_KEY='...'     # 不要写进任何文件
export RUNBOOKGUARD_LLM_MAX_CALLS=8       # 硬上限
apps/agent-runtime-python/.venv/bin/python scripts/validate-m3-real-provider.py

# 清理
cd deploy/compose && docker compose down -v
```

---

## 8. 关于凭据的一点说明

本次真实验证使用了你提供的 API key。它的处理方式：

- 只通过环境变量传入，**未写入仓库中任何文件**（源码、配置、artifact、本报告均无）。
- artifact 中的所有响应文本都过了 `redact()`。
- `ProviderConfig.__repr__` 遮盖它，因此它不会出现在日志或异常信息里。
- 未提交 git。

**但请注意：这个 key 已经出现在我们的对话记录里。** 如果它有实际额度或权限，建议在完成验证后到服务商后台轮换它。这不是我能替你做的操作。

---

## 9. 简历状态

**仍不允许写入简历。** M6 Gate 未通过。M3 完成的是模型接入的失败语义，不是产品能力证明。特别地，「真实模型验证通过」只意味着这条链路能通，**不意味着诊断质量达标**——那要等 M6 的冻结评测。
