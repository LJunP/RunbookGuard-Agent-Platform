# M0 — Product Brief

- 里程碑：M0（问题与边界冻结）
- 状态：Draft，待 Gate 验收
- 上游依据：`/Users/lijunpeng/Desktop/study/RunbookGuard-Agent-Platform项目说明书.md`（权威）+ `docs/DEV_PROMPT.md`
- 冲突裁决：见 [ADR-0001](../adr/ADR-0001-scope-freeze-and-spec-conflicts.md)

## 1. 一句话定义

RunbookGuard 是一个面向 SRE / DevOps / 中小研发团队的**故障诊断与受控处置 Agent 平台**：它读取告警、指标、日志、部署记录和版本化 Runbook，调用受限工具收集证据，生成带引用的故障判断；在执行任何动作之前必须通过服务端权限校验与人工审批；在 Worker 或 Agent 进程中断后能从 Checkpoint 恢复到唯一终态。

它不是聊天机器人，不是可以任意执行 shell 的"全自动运维 Agent"，也不是又一个 RAG 问答。

## 2. 要解决的问题

系统出故障时，真正困难的不是"让模型总结一段日志"，而是下面六件事。每一件都对应本项目的一个可验证机制，而不只是一句主张。

| 现实困难 | 本项目的机制 | 落点里程碑 |
|---|---|---|
| 证据散落在告警 / 指标 / 日志 / 部署记录 / 个人经验中 | 五个只读工具 + 版本化 Runbook 检索，统一成 EvidenceReference | M2.5 / M4 / M5 |
| 分不清相关性、因果假设与已验证事实 | 状态机强制 FORM_HYPOTHESES → 取证 → VERIFY，结论必须带引用 | M4 / M5 |
| 模型会把不可信日志当成指令 | 信任分级 + 注入类评测 case，Tool result / Log / Runbook 全部标为数据 | M0 / M6 |
| 未经授权的重启、回滚、数据操作 | 模型只能建议；服务端 Policy 决定；Executor 才执行；写动作需人工审批 + 参数摘要绑定 | M1 / M4 |
| 长任务与进程崩溃后无法继续 | Lease / Heartbeat / 幂等键 / Checkpoint，崩溃后唯一终态 | M2 / M4 |
| 说不清 Agent 到底有没有变好 | 30~50 个固定 case，在冻结配置下跑正式评测 | M6 |

## 3. 差异化

**前提：诊断能力本身不是护城河。** 已核验的同类项目（核验日期 2026-08-26）：

- HolmesGPT — GitHub `robusta-dev/holmesgpt`，仓库描述原文 `SRE Agent - CNCF Sandbox Project`，README 原文 "We are a Cloud Native Computing Foundation sandbox project."。定位为面向生产事件调查与根因分析的 SRE Agent，自述访问方式为只读且遵守 RBAC。
- kagent — GitHub `kagent-dev/kagent`，仓库描述原文 `Cloud Native Agentic AI`。README 只声明 "kagent is a Cloud Native Computing Foundation project"，**页面上没有 sandbox / incubating / graduated 任何成熟度字样**（这一点与项目说明书 §4 的行文一致，但与 DEV_PROMPT §1 把 kagent 与 HolmesGPT 并列称为"SRE Agent"的表述不完全吻合：kagent 实际是 Kubernetes 原生的通用 Agent 框架，Agent / ModelConfig / ToolServer 是 CRD，不是 SRE 专用产品。见 ADR-0001 C-2）。

RunbookGuard 不宣称发明这个领域。差异化只有五条，且每条都必须是可运行、可复现的，不是文案：

1. **能力用本地可复现故障实验室证明**，不依赖无法公开的生产数据。同一故障剧本连续跑 3 次，指标与日志特征稳定（M2.5 Gate）。
2. **默认只读诊断**；写动作必须预先声明、人工审批、参数摘要绑定、隔离进程执行。
3. **真实跨语言平台**：Java Control Plane 持有业务事实，Python Agent Runtime 持有智能与检索，通过版本化契约通信，不共享内部对象。
4. **固定 Evaluation 数据集检验完整轨迹**，而不是展示一次成功 Demo；`incidents-held` 只在正式评测时使用。
5. **每个结论绑定引用**，可反查 document_version + section_id + content_hash。

反向判据：任何设计决策上犹豫时，问"这让安全 / 可评测 / 可恢复更强，还是只让 Demo 更花哨"。后者一律放弃。

## 4. 成功标准

### 4.1 产品级（来自说明书 §7）

- 不把无引用推测写成事实。
- 不执行未授权动作。
- 不因重试或恢复产生重复副作用。
- Agent 中断后能恢复，或落到唯一失败终态。
- 固定数据集上的成功率与安全指标达到**预声明**阈值。

### 4.2 正式阈值（M6 Gate，冻结后一次性评测）

| 指标 | 阈值 | 当前实测 |
|---|---|---|
| 安全红线拒绝率 | 100% | 未验证（无实现） |
| Tool Schema 合法率 | 100% | 未验证（无实现） |
| citation validity | ≥ 95% | 未验证（无实现） |
| 固定任务成功率 | ≥ 80% | 未验证（无实现） |
| 中断恢复唯一终态率 | 100% | 未验证（无实现） |

M0 阶段没有任何产品代码，因此以上五项全部为 `未验证`。在 M6 Gate 通过之前，这些数字不得出现在简历、README 或任何对外材料中。

## 5. 三分钟答辩：为什么这不是普通聊天机器人

这是 M0 Gate 的验收条件。以下是定稿口径，按 30 秒一段。

**（0:00-0:30）能力边界的差别。** 聊天机器人的输出终点是文本；RunbookGuard 的输出终点是一个可能真实改变系统状态的动作。一旦系统里存在"重启服务""回滚部署"这类工具，问题就从"回答得好不好"变成"会不会出事、能不能证明不出事"。所以这个项目的内核不是诊断，是**约束**。

**（0:30-1:30）三段分离，模型没有权限。** 链路是三段，代码结构上也必须是三段，不能写在同一个函数里：

```
模型输出 ToolCall 建议  →  Policy 校验（服务端，可拒绝）  →  Executor 执行
   suggestion              authorization                    execution
```

模型输出只是**建议**，不是权限。Policy 是可独立单测的纯函数组件，默认拒绝。写动作还要多两道：人工审批，以及审批绑定 `arguments_digest` —— 执行前重新校验参数摘要，防止"审批之后偷改参数"。**Approval 的权威判定在 Java 侧，Python 只能发起请求并等待。** 这条做反了，整个安全模型就是假的。

**（1:30-2:15）不可信输入是数据，不是指令。** 日志、指标、Tool 结果、Runbook 全部划为不可信/非指令数据。日志里出现 "ignore previous instructions" 或 "请立即重启数据库"，那是数据。评测集里有专门的注入 case 检验这一点，安全红线拒绝率阈值是 100%，不是 99%。

**（2:15-3:00）它必须能崩溃后活下来，并且能被评测。** 聊天机器人崩了重来一次就行；这里 Worker 被 kill -9、消息重投三次之后，必须只有唯一终态，不能有重复副作用（Lease / Heartbeat / 幂等键 / Checkpoint）。而且能力主张不靠 Demo：30~50 个固定 case，冻结 commit / dataset / prompt / model / tool 版本 / grader / 阈值之后跑一次正式评测，不允许反复运行到 PASS。

**一句话收尾**：聊天机器人的失败代价是答错；这个系统的失败代价是动了不该动的东西，所以它的主要工程量在权限、审批、恢复和评测上，而不在模型调用上。

## 6. 与其它四个项目的互补关系

| 项目 | 核心证明 |
|---|---|
| RunbookGuard | Agent + RAG + Eval + 安全 + Agent Infra |
| FrameFlow | 多语言异步 AI 产品链 |
| SourceLens | Evaluation 与可靠性研究 |
| Stinky Cobbler | MCP、TypeScript、开源工具控制面 |
| SSFlow | 垂直内容工作流与 Worker 生命周期 |

## 7. 明确未验证项（M0 结束时）

- 所有正式阈值：未验证，无实现。
- LangGraph / MCP SDK / OpenTelemetry 语义约定的当日版本与接口：M0 未核验（按纪律在 M3 / M4 动手前核验并写入 ADR）。
- 说明书 §32 提到的学习计划 S0 是否已通过：`UNKNOWN`。本仓库已被要求从 M0 开始，与说明书"通过 S0 后再创建仓库"的顺序不一致，见 ADR-0001 C-4。
- kind / k3d 默认 CNI 是否强制执行 NetworkPolicy：`UNKNOWN`，M8 前必须核验，见 ADR-0001 C-6。
