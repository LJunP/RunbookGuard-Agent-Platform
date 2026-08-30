# ADR-0001：M0 范围冻结与上游文档冲突裁决

- Status: Accepted（M0 阶段生效，M1 开始前可因新事实修订）
- Date: 2026-08-26
- Deciders: 项目所有者（验收人）+ 主力开发工程师
- 里程碑：M0
- 关联：[Product Brief](../architecture/M0-product-brief.md)、[不做清单](../architecture/M0-non-goals.md)、[威胁模型](../architecture/M0-threat-model.md)、[初始故障场景](../architecture/M0-initial-incident-scenarios.md)

## Context

本项目有两份上游文档：

1. **项目说明书**：`/Users/lijunpeng/Desktop/study/RunbookGuard-Agent-Platform项目说明书.md`（含 frontmatter，创建/更新日期 2026-08-25，项目状态"规划完成尚未创建"）。
2. **开发主提示词**：`docs/DEV_PROMPT.md`，自述为"本提示词的上游依据是说明书"。

DEV_PROMPT §2 明确规定：**两者冲突时以说明书为准，并指出冲突在哪**。本 ADR 记录逐条比对后发现的冲突与差异，以及裁决结果。这不是文档洁癖——这些差异中有几条会直接影响 M2.5 的工程量和 M6 的评测口径，现在不裁决，后面会以"实现与文档不一致"的形式爆出来。

## Decision

**裁决原则**：以说明书为准；当 DEV_PROMPT 是**纯增量**（说明书未涉及、且不与说明书矛盾）时采纳增量，但必须在本 ADR 留痕；当二者**语义矛盾**时采用说明书，除非增量方向能被证明更有利于"安全 / 可评测 / 可恢复"，此时需显式记录偏离。

---

## 冲突与差异清单

### C-1 M2.5 Synthetic Lab 里程碑：DEV_PROMPT 新增，说明书无

**说明书**：§23 的里程碑序列是 M0→M8，没有 M2.5。合成故障环境散落在 M6（评测）与 M8（故障演练）中隐含存在，§8 只提到"所有处置先针对本地合成实验环境"。

**DEV_PROMPT**：§12 插入 M2.5，自述"我在原规划上追加的里程碑"，理由是整个评测、演示、故障演练都依赖它，且工程量不小于 Agent Runtime，不提前做 M6 必然卡死。

**裁决**：**采纳 DEV_PROMPT 的增量。** 这不是范围扩大，而是范围**前移**——synthetic-lab 本来就在说明书的必做范围内（§8、§17、§20 都依赖它），只是没有独立里程碑。前移的收益是明确的：M4 的五个只读工具需要真实数据源才能写出有意义的契约与测试，否则只能对着 mock 写，违反"不用 mock 冒充真实"的纪律。

**影响**：M2.5 成为 M4 的前置依赖。里程碑序列变为 M0→M1→M2→M2.5→M3→M4→M5→M6→M7→M8。

### C-2 对 HolmesGPT / kagent 的描述不准确

**说明书**：§4 表述为"HolmesGPT 是 CNCF Sandbox 的 SRE Agent""kagent 聚焦 Cloud Native Agentic AI、Kubernetes 与 MCP"。§31 给出 HolmesGPT 链接为 `https://github.com/HolmesGPT/holmesgpt`。

**DEV_PROMPT**：§1 表述为"HolmesGPT（CNCF Sandbox）和 kagent 已经在做 SRE Agent"，把两者并列为 SRE Agent。

**当日核验结果（2026-08-26，WebFetch）**：

- HolmesGPT 实际仓库是 `robusta-dev/holmesgpt`（说明书 §31 的 `HolmesGPT/holmesgpt` 链接与实际组织名不一致）。仓库描述原文 `SRE Agent - CNCF Sandbox Project`，README 原文 "We are a Cloud Native Computing Foundation sandbox project."。→ 说明书对 HolmesGPT 的定性**准确**，仅链接地址需更正。
- kagent 实际仓库 `kagent-dev/kagent`，描述原文 `Cloud Native Agentic AI`。README 仅称 "kagent is a Cloud Native Computing Foundation project"，**页面上没有任何成熟度级别字样**（无 sandbox / incubating / graduated）。其形态是 Kubernetes 原生的**通用 Agent 框架**：Agent / ModelConfig / ToolServer 均为 CRD，附带 K8s、Istio、Helm、Argo、Prometheus、Grafana、Cilium 工具集。→ 说明书 §4 的表述**准确**；DEV_PROMPT §1 把 kagent 归为"SRE Agent"**不准确**。

**裁决**：以说明书为准。对外材料中的定性统一为：HolmesGPT 是 CNCF Sandbox 的 SRE Agent；kagent 是 CNCF 的云原生 Agent 框架（不声明成熟度级别）。§31 的 HolmesGPT 链接更正为 `https://github.com/robusta-dev/holmesgpt`。

**为什么这条重要**：差异化叙述建立在对同类项目的准确认知上。在面试现场把 kagent 说成 SRE Agent，会被一句"你真看过吗"直接击穿。竞品描述必须可核验，且标注核验日期——CNCF 成熟度级别是会变的，本结论的有效期仅限 2026-08-26。

### C-3 动作工具集合：说明书 4 项示例 vs DEV_PROMPT 固定 3 项

**说明书**：§8 列出"例如"四项：重启指定 synthetic service、**暂停指定测试消费者**、回滚到预先存在的测试镜像、降低 synthetic traffic generator 速率。措辞是"例如"，未固定数量。§14 的"审批型本地动作工具"则只列三项：`restart_synthetic_service`、`rollback_synthetic_deployment`、`throttle_synthetic_traffic`。**说明书内部即存在不一致**：§8 的"暂停消费者"在 §14 中没有对应工具。

**DEV_PROMPT**：§7 与 §14 均固定为三项，并明确"只允许固定的三个动作工具"。

**裁决**：**采用三项**（`restart_synthetic_service` / `rollback_synthetic_deployment` / `throttle_synthetic_traffic`）。依据是说明书 §14 的正式工具清单优先于 §8 的举例性表述，且 DEV_PROMPT 与 §14 一致。

"暂停测试消费者"的语义可由 `throttle_synthetic_traffic`（速率降为 0）覆盖，无需第四个工具。工具集越小，Policy 的可穷举性越强——这与 NG-1 的理由同源。

**影响**：M2 场景 S2（MQ backlog）的处置动作为 `throttle_synthetic_traffic`，不新增 pause 工具。若后续发现 throttle 无法表达"暂停消费者"（因为 throttle 作用于生产端而非消费端），需回到本 ADR 重新裁决，不允许在实现中悄悄加第四个工具。

### C-4 开发起点：说明书要求先过 S0，DEV_PROMPT 要求现在开始 M0

**说明书**：§22 "当前只规划，不在 Study 中创建该仓库"；§32 "下一步不是让 AI 一次生成项目，而是先完成学习计划 S0。通过 S0 后，再依据 M0 创建 RunbookGuard 仓库和第一个最小里程碑"。

**DEV_PROMPT**：§2 声明仓库已存在（git 仓库，主分支 main，只有单行 README 和一次 Initial commit）；§16 要求"确认当前处于 M0，然后开始工作"。

**实际状态（已核验）**：仓库确实存在于 `/Users/lijunpeng/Desktop/open_source_project/RunbookGuard-Agent-Platform`，`git log` 只有一条 `00a76cd Initial commit`，工作区只有 `README.md`（内容为单行 `# RunbookGuard-Agent-Platform`）和未跟踪的 `docs/`。

**裁决**：**按 DEV_PROMPT 执行 M0**，但记录偏离。理由：① 仓库已由项目所有者创建，说明书 §22 的"不创建"前提已被所有者本人的行动推翻；② M0 的交付物全部是文档（Product Brief / 用户与场景 / 不做清单 / 威胁模型 / 8 个故障场景），不写任何产品代码，与"先学习再编码"的意图不冲突——M0 本身就是需求与边界澄清阶段。

**未验证项**：S0 学习计划是否已通过：`UNKNOWN`。这一点只有项目所有者能回答。若 S0 未通过，M0 仍可继续（纯文档），但 **M1 开始前应确认 S0 状态**，因为 M1 起就要写真实的 Java + MySQL + Redis 代码。这一条列为 M1 前置依赖。

### C-5 M2.5 数据接口需要第五类：容器/运行时事件

**DEV_PROMPT**：§12 M2.5 交付物列出四类可查询接口——"指标 / 日志 / 部署记录 / 队列状态"，并称其为"只读工具的数据来源"。

**冲突来源**：DEV_PROMPT 同时要求 M2.5 先实现 4 类故障，其中包含 **OOMKilled**（对应故障场景 S4）。OOMKilled 的必需证据包括容器重启次数与 OOMKilled 事件记录，这既不属于指标时序，也不属于应用日志（重启会造成日志断裂，这本身是证据），更不属于部署记录或队列状态。说明书 §20 也把 OOMKilled 列为必须演练项。

**裁决**：**M2.5 增加第五类可查询接口：容器/运行时事件**（字段至少含 service、event_type、timestamp、restart_count、exit_code / reason）。这不是范围扩大，而是补齐 M2.5 已声明故障类型所必需的数据面——不补齐则 S4 无法成立，M6 的 13 类覆盖也不成立。

**影响**：M2.5 交付物中的接口从 4 类变为 5 类。第一批只读工具是否需要第 6 个工具（`get_runtime_events`）来暴露这类数据，**留待 M4 决策**：可选方案是扩展 `get_service_metrics` 的返回结构，或新增独立工具。倾向于新增独立工具（保持单一工具单一语义，便于 Policy 判定），但需在 M4 的工具契约设计中正式决定并记入新 ADR。注意说明书 §14 与 DEV_PROMPT §7 都把只读工具固定为 5 个，若确定新增第 6 个，属于对上游清单的偏离，必须显式记录。

### C-6 "进程内运行只读工具" 与 "每个工具必须支持 cancel" 的技术张力

**说明书**：§18 Sandbox 分级规定"第一阶段只允许只读工具在进程内运行"；§14 同时要求**每个**工具契约必须包含 `timeout、cancel、result size`。DEV_PROMPT §7 与 §9 重复了这两条要求。

**冲突点**：进程内执行的同步调用无法被真正强制取消。Python 中 `asyncio.Task.cancel()` 只在 await 点生效，若工具实现中存在阻塞调用（同步 HTTP、CPU 密集解析），取消请求会被无限期推迟；线程也不能被安全强杀。也就是说"进程内 + 可取消"在一般情况下不可同时满足。

**裁决**：把 `cancel` 的语义**分级定义**，而不是假装所有工具都能硬取消：

- **只读工具（进程内）**：`cancel` 定义为"协作式取消 + 超时兜底"。要求所有只读工具实现为全 async 且不含阻塞调用（HTTP 客户端必须是异步的），取消请求在下一个 await 点生效；超过 timeout 后 Run 侧**放弃等待并标记 typed failure**，不等待工具真正结束。工具契约中必须显式声明 `cancel_semantics: cooperative`。
- **动作工具（独立进程/容器）**：`cancel` 为强制取消（终止进程），`cancel_semantics: forceful`。

**放弃等待带来的残余风险**：被放弃的只读工具协程仍可能继续占用资源直到自然结束。缓解措施是只读工具必须有自身的 HTTP timeout（小于 Run 侧 timeout）与结果大小上限。此残余风险在 M4 工具契约中显式记录，不隐藏。

**为什么不直接把只读工具也放进独立进程**：会显著增加每次工具调用的开销与实现复杂度，而只读工具的风险等级为 `read`，说明书已判定进程内可接受。这是有意识的权衡，不是妥协。

### C-7 其它已核对无冲突项（记录以证明核对范围）

以下项目在两份文档间语义一致，无需裁决：技术栈（Java 17 / Spring Boot / MyBatis / MySQL 8 / Redis / RabbitMQ / Python 3.12 / FastAPI / Pydantic / pytest / LangGraph / Qdrant / TypeScript+React / OpenTelemetry / Prometheus / Grafana / Docker Compose / kind 或 k3d / OpenAI-compatible Adapter）；六个领域模型的字段清单；Agent 状态机与八条终止条件；工具契约 11 项；MCP 边界六条；信任分级七行；强制控制清单 12 项；五个正式阈值；冻结项七项；测试金字塔与四条 E2E 主线；M6 前不得写入简历。

DEV_PROMPT 相对说明书的**纯增量**（采纳并留痕）：M5 结束时先用 `incidents-dev` 做非正式基线摸底（说明书无此要求，但它能避免 M6 首次冻结才发现指标不达标——这是对"不允许反复运行到 PASS"纪律的必要配套，否则纪律会逼人在唯一一次机会上赌）；`incidents-dev` 与 `incidents-held` 的使用纪律细化；contracts 目录三分（http/events/tools）。

## Consequences

**正面**：

- 里程碑序列与 M2.5 数据接口范围在写代码前确定，M4 不会因缺数据源返工。
- 竞品描述可核验且带核验日期，差异化叙述站得住。
- `cancel` 语义分级避免了在工具契约里写下无法实现的承诺（这本身就是"不许假装成功"的应用）。

**负面 / 代价**：

- M2.5 工程量比 DEV_PROMPT 原描述多一类接口。
- 只读工具的 `cooperative` 取消语义留下残余风险，需在 M4 契约中显式声明，无法完全消除。
- 若 M4 决定新增第 6 个只读工具，会与说明书 §14 的 5 个工具清单产生偏离，需要新 ADR。

**需要项目所有者确认的一项**：C-4 中 S0 学习计划的通过状态（`UNKNOWN`）。这不影响 M0，但影响 M1 是否应该现在开始。

## 后续待核验项（版本敏感，按铁律五在动手前核验）

| 项 | 影响里程碑 | 状态 |
|---|---|---|
| LangGraph 当日 API（node/edge/state/checkpointer/interrupt 接口形状与持久化后端） | M4 | `UNKNOWN`，M4 前核验并写 ADR |
| MCP 当日正式协议版本、SDK 支持情况与迁移说明 | M4 | `UNKNOWN`，M4 前核验并写 ADR |
| OpenTelemetry GenAI 语义约定的当前稳定性（experimental 与否） | M7 | `UNKNOWN`，M7 前核验 |
| kind / k3d 默认 CNI 是否强制执行 NetworkPolicy | M8 | `UNKNOWN`，M8 前核验（默认 kindnet 历史上不执行 NetworkPolicy，若成立则需换 CNI，否则 §20 的 NetworkPolicy 要求无法验证） |
| 参数规范化 / `arguments_digest` 算法选型 | M1 | `UNKNOWN`，M1 决策并写 ADR |
| 动作工具沙箱实现方式（独立进程 vs 容器） | M4 | `UNKNOWN`，M4 决策并写 ADR |
