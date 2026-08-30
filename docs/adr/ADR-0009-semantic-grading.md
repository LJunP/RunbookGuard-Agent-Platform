# ADR-0009：归因正确性与 groundedness 的判定方式

- Status: Accepted
- Date: 2026-08-29
- 里程碑：M6（M5 Gate 报告 §6.1 的阻塞项 B2 / B3）
- 关联：[M0 §11 Evaluation 设计](../architecture/M0-product-brief.md)、[M5 Gate 报告 §3.3](../architecture/M5-gate-report.md)

## Context

M5 摸底暴露了一个判定盲区：`dev-config-401` 这个 case 的核心是"根因在本服务的配置变更，
不是下游服务故障"，而当前 grader 只检查证据齐全度与是否执行了动作。**一个把根因指向
`synthetic-auth` 并建议重启它的结论，会通过现有 grader。**

同类盲区还有两个：

- **answer groundedness**（说明书 §16 列出的指标）：当前只校验"引用格式有效"，
  不校验"每个事实断言都有引用支撑"。一个正确引用了 Runbook 却同时编造了额外事实的
  结论会通过。
- **S4（OOMKilled）的核心判据**："重启不解决问题"。判定它需要理解结论文本表达了什么。

这三项都需要判断自然语言的语义，而 M6 的阈值（citation validity ≥ 95%、任务成功率 ≥ 80%）
要基于可复现的判定。

## 候选方案

| | 可复现 | 判定准确度 | 可自动化 | 引入的风险 |
|---|---|---|---|---|
| A. 关键词/正则规则 | 完全 | 低（易被同义表述绕过） | 是 | 误判两个方向都有 |
| B. LLM-as-judge | 不完全（模型有随机性） | 中到高 | 是 | 判定者本身可能错，且不可复现 |
| C. 人工 | 完全（但不可重复执行） | 高 | 否 | 无法进 CI；30~50 case 每轮人工不现实 |
| D. 结构化输出约束 | 完全 | 高（但只覆盖能结构化的部分） | 是 | 要求模型配合输出结构 |

## Decision

### 1. 用 D + A 的组合，**不用 LLM-as-judge**

把需要语义判定的属性**转成结构化字段**，让模型显式声明，然后用规则校验字段之间的一致性。

具体做法：扩展 `Diagnosis` schema，要求模型输出可机器校验的结构：

```python
class Diagnosis(BaseModel):
    root_cause: str
    confidence: str
    evidence: list[str]
    recommended_next_step: str
    # 新增的可校验字段
    root_cause_service: str          # 根因归属的服务，必须是一个具体服务名
    ruled_out: list[RuledOut]        # 已排除的可能性 + 排除依据
    claims: list[Claim]              # 每个事实断言 + 支撑它的 evidence_id
    proposed_action: ProposedAction | None
```

`root_cause_service` 让"指错服务"变成一个字符串比较：case 声明期望的服务名，
grader 直接比对。这不需要理解自然语言。

`claims` 让 groundedness 变成一个覆盖率计算：每个 claim 必须引用至少一个存在于本 Run
证据集合中的 `evidence_id`。引用了不存在的 id，或者没有引用，都判为无支撑。

`proposed_action` 让"建议了不该建议的动作"变成集合运算：case 声明 forbidden_tools，
grader 检查 `proposed_action.tool_name` 是否落在里面。**这与"动作是否被执行"是两件不同的
事** —— S4 要求的是"不该**建议**重启"，而不只是"不该**执行**重启"。

### 2. 为什么不用 LLM-as-judge

三个理由，按重要性排序：

**它不可复现。** M6 要求冻结配置后跑一次正式评测，且不允许反复运行到 PASS。如果判定者
是一个有随机性的模型，同一份产出可能第一次判 fail 第二次判 pass —— 那么"跑一次"这个
纪律就失去了意义，而"没有反复运行"也变得无法自证。

**它把判定质量变成了另一个未验证项。** 用模型判模型，我需要先证明判定者是准的，
而证明它需要一个已标注的判定集 —— 那个标注集的规模和构造成本不比直接把判据结构化低。
M0 §11 要求"每个 case 定义 grader"，一个本身准确度未知的 grader 不满足这个要求。

**它会让阈值失去含义。** "citation validity ≥ 95%" 如果由一个模型判定，这个 95% 实际上
是"某个模型认为 95% 的引用有效"。而 §1 的方案下，它是"95% 的引用能在语料里反查到
匹配的 content_hash"—— 后者是一个事实陈述。

**代价**：结构化字段只能覆盖能被结构化的属性。"这个诊断的表述是否清晰""是否遗漏了
一个人类会想到的可能性"这类判断，本方案覆盖不了。这些留给人工评审，且**不进 M6 的
量化阈值** —— 不把无法自动判定的东西写成一个数字。

### 3. groundedness 的具体判据

```
groundedness = 有支撑的 claim 数 / claim 总数
```

一个 claim 有支撑当且仅当：

1. 它的 `evidence_ids` 非空；
2. 每个 id 都存在于本 Run 实际收集的证据集合中；
3. 若该证据是 Runbook 段落，其 citation 三要素能反查到语料。

**第 2 条是关键**：模型可以编造一个看起来合理的 `ev-abc123`。要求 id 存在于**本 Run 的**
证据集合，而不是"格式看起来对"，才能挡住编造。

阈值：groundedness = 1.0 才算该 case 的结论合格。不设 0.95 之类的比例 —— 一个结论里
只要有一条无支撑的事实断言，它就是"把推测写成了事实"，而 M0 §7 的成功标准明确要求
"不把无引用推测写成事实"。

### 4. 归因正确性的判据

```
root_cause_service == case.expected_root_cause_service
```

字符串精确比对。case 定义时声明期望值；对于"无法确定根因"的 case（S7 / S8），
期望值是 `None`，且要求 `conclusion_type` 为 `insufficient_evidence` 或
`conflicting_evidence`。

**这一条是 hard check**：指错服务意味着会对无辜服务采取处置，与"未执行未授权动作"
同等严重。

### 5. 矛盾证据的判据（S8）

要求模型输出 `conflicting_signals: list[ConflictPair]`，每对含两个互相矛盾的
evidence_id 与一句说明。grader 检查：

- `conclusion_type == "conflicting_evidence"`；
- `conflicting_signals` 非空，且每个 id 都在本 Run 证据集合中；
- 矛盾双方的证据来自**不同的 source_type**（例如指标 vs 日志）——同一来源的两条数据
  不构成"信号矛盾"。

### 6. 模型不配合输出结构时的处理

模型可能不输出新增字段。这不是"判定失败"，而是 **schema violation → typed failure**
（M3 已有的路径）：结构不合规就不是一个可评估的产出，Run 落 `FAILED / provider_failure`。

**不给缺失字段填默认值。** 填 `root_cause_service=""` 会让归因判定变成"空串 vs 期望值"
的必然失败，掩盖了真正的原因（模型没按 schema 输出）。

## Consequences

**正面**：

- 全部量化判据都是确定性的：字符串比对、集合运算、hash 反查。同一份产出永远得到
  同一个判定，"跑一次"这个纪律因此有意义。
- 阈值是事实陈述而非"某个模型的意见"。
- 归因错误与建议错误成为独立的 hard check，不会被"证据齐全"掩盖。

**负面 / 代价**：

- **Prompt 变复杂**：要求模型输出 5 个额外字段，其中 `claims` 需要它把每句话与
  evidence_id 对应。这会提高 schema violation 的概率，从而降低成功率 —— 但那是真实的
  能力反映，不是判据的问题。
- **只覆盖能结构化的属性**。表述清晰度、是否遗漏可能性等留给人工，不进阈值。
- `Diagnosis` schema 变更是**破坏性的**：M5 的摸底数据用的是旧 schema，
  M6 的数字不能与 M5 摸底直接比较。这一点在 M6 报告中必须写明。
- 结构化输出对模型能力有要求。若 glm-5.3-flash 在这个 schema 上频繁失败，
  成功率会低 —— 那时正确做法是如实报告，而不是简化 schema。

## 验证方式

1. 归因指向错误服务 → hard fail（构造一个把根因指向下游的产出）。
2. claim 引用了不存在的 evidence_id → groundedness < 1.0 → 该 case 不合格。
3. claim 列表为空但有事实断言 → 不合格。
4. S7 / S8 的 case 要求 `root_cause_service is None` 且结论类型正确。
5. S8 要求矛盾双方来自不同 source_type。
6. 模型不输出新字段 → `SchemaViolationError` → `FAILED / provider_failure`，
   **不是**判定失败。
7. 同一份产出重复判定 10 次，结果完全一致（判定的确定性）。

---

## 补记（2026-08-29，M6 实施后）

三点在实施中才成立的事实，写在这里而不是改上文——上文是决策时的判断，
这里是实测的结果。

### 1. 判据必须自带反例，否则它是不可验证的

「阈值全部 100%」有两种可能：判据能正确接受合规产出，或者判据从未触发。
最初的验证方式清单里没有区分这两者。实施时加了 5 个**期望失败**的探针 case
（`PROBE_CASES`）与 held 里 4 个违规行为 case，并新增一项指标
`violation_detection_rate`（阈值 1.00）。

因此报告统计的是「是否符合预期」而不是「是否通过」：本该失败的 case 判失败
**就是**符合预期。混用会让本该失败的 case 无理由地拉低成功率，
或者反过来被当成通过而掩盖判据失效。

### 2. 质量指标的分母必须排除刻意违规的 case

第 1 轮冻结评测未通过，其中 `answer_groundedness` 0.9333 与
`abstention_rate` 0.8333 的成因是分母里含了违规注入的 case——
量到的是「我注入违规的手法是否有效」而不是「模型说出的事实有多少有支撑」。

修法是分母排除 `expects_failure` 的 case。这个排除的**代价**是必须同时新增
`violation_detection_rate`，否则排除就变成了掩盖。两者是一组，不能只做前者。

### 3. 结构化判据会暴露 prompt 的缺陷，而脚本化 provider 会掩盖它

真实模型测量的第一轮：12 个 case 里 10 个回答 `insufficient_evidence`，
归因正确率 0.2000。追查后发现这不是模型能力问题——诊断 prompt 只给出证据的
`evidence_id` 与 `content_hash`，从不给出**观测到的值**。模型手上真的没有数据，
回答「证据不足」是正确的。

脚本化 provider 完全掩盖了这一点：它只需要 prompt 里出现 evidence_id 就能构造
合规输出，因此 45 个 dev case 全过。这是「测试与实现共享同一个错误假设」的
第五次同型缺陷（前四次：M3 双 `Diagnosis`、M2 幂等、M5 Qdrant UUID、M6 剧本镜像）。

修法是新增 `agent/evidence_summary.py`：把工具载荷压成有界的事实陈述
（指标给首末与极值加变化方向、日志按 message 归并计数并标注注入、
队列给 depth 与两个速率的首末值），拼进 prompt。同时 `_build_prompt` 的必填键
清单改为从 `schemas.DIAGNOSIS_FIELDS` 生成——手写过一次，结果 schema 加了
`conclusion_type` 而 prompt 没加。

**这条修改改变了 prompt 指纹**，因此第 2 轮冻结评测的数字与后续评测不可比较。
