# ADR-0006：LangGraph API 核验与编排边界

- Status: Accepted
- Date: 2026-08-27
- 里程碑：M4（M3 Gate 报告 §6.1 的阻塞项 B2）
- 关联：[M0 Agent 状态机](../architecture/M0-product-brief.md)、[ADR-0003 Lease](ADR-0003-lease-and-idempotency.md)

## Context

铁律五要求版本敏感的东西动手前当场核验，不凭记忆写 API。LangGraph 的接口在过去一年
变动频繁（`MemorySaver` → `InMemorySaver`、`interrupt` 的语义从 `interrupt_before`
配置式改为函数式），因此本 ADR 先记录**实测**的 API 形状，再定编排边界。

DEV_PROMPT §12 M4 另有一条要求：**先手写一遍最小有界执行循环，再换成 LangGraph**，
目的是能解释每个抽象替你做了什么，而不只是会调 API。本 ADR 记录这个对照结论。

## 核验结果（2026-08-27 实测）

版本：`langgraph==1.2.11`、`langgraph-checkpoint-sqlite==3.1.1`（均为当日 PyPI 最新）。

以下全部由 `inspect` 在已安装的包上直接探测得到，非来自文档摘抄：

```
langgraph.graph 导出：END, MessageGraph, MessagesState, START, StateGraph,
                     add_messages, message, state

StateGraph 方法：add_node / add_edge / add_conditional_edges / compile /
                set_entry_point  全部存在

compile 签名（实测）：
  compile(self, checkpointer: Checkpointer = None, *,
          cache: BaseCache | None = None,
          store: BaseStore | None = None,
          interrupt_before: All | list[str] | None = None,
          interrupt_after: All | list[str] | None = None,
          debug: bool = False,
          name: str | None = None,
          transformers: ... | None = None) -> CompiledStateGraph[...]

from langgraph.types import interrupt, Command      -> OK
  interrupt(value: Any) -> Any
  Command 字段：graph, update, resume, goto, PARENT

from langgraph.checkpoint.memory import InMemorySaver          -> OK
from langgraph.checkpoint.sqlite import SqliteSaver            -> OK
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver   -> OK
```

**与官方文档页的差异**：`docs.langchain.com/oss/python/langgraph/overview` 只展示了
`StateGraph / add_node / add_edge / compile / invoke` 与 `START / END`，没有提及
`add_conditional_edges`、`interrupt`、`Command` 或任何 checkpointer 类。persistence 页
给出了 `InMemorySaver` 与 `PostgresSaver` 的导入路径，但只提到 `SqliteSaver` 的名字而
未给路径。**上面的 SqliteSaver 路径是实测得到的，不是从文档推测的。**

## Decision

### 1. 状态机的权威定义在我们的代码里，不在 LangGraph 里

M0 §6 定义了 14 个状态与 8 条终止条件。这些是**业务事实**，因此：

- `RunStatus` 枚举与转移合法性判定写成独立的、不依赖 LangGraph 的纯 Python 模块。
- LangGraph 的节点只是"执行某个状态对应的工作"的载体。
- 终止条件的判定（预算、deadline、repeated-state）在我们的 `BoundedLoopGuard` 里，
  **不用 LangGraph 的 `recursion_limit`**。

**为什么不用 `recursion_limit`**：它只能表达"最多走多少步"，而我们需要 8 种不同的
终止原因各自映射到一个 `failureClass`（M6 的失败归因统计依赖这个）。
`recursion_limit` 触发时抛的是框架异常，说不出是 max_steps 还是 deadline。

代价：重复实现了一部分 LangGraph 已有的能力。收益：终止原因是我们自己的领域概念，
可测、可归因、可跨语言上报。

### 2. 先手写最小有界执行循环，LangGraph 之后接管编排

交付顺序（DEV_PROMPT §12 M4 要点）：

1. `bounded_loop.py`：不依赖 LangGraph 的最小有界执行循环。8 条终止条件全部在这里，
   全部有单测。
2. `graph.py`：用 LangGraph 重新表达同一状态机，**复用**第 1 步的守卫与状态判定。
3. 一致性测试：同一输入下两条路径的终态与 failureClass 必须相同。

**第 3 步是这个顺序的意义所在**。它把"LangGraph 替我做了什么"变成一个可执行的答案：
两条路径行为一致的部分是我自己实现的（守卫、状态转移），只有 LangGraph 那条具备的
才是框架的贡献（检查点持久化、interrupt/resume、并发节点调度）。

### 3. Checkpoint：SQLite 用于 M4，权威版本信息仍在 MySQL

- LangGraph 侧用 `AsyncSqliteSaver`，落盘到本地文件。
- **同时**在 Java Control Plane 的 `checkpoint` 表（M1 已建）写一条记录，
  含 `graph_version`、`state_schema_version`、`sequence`、`state_digest`。

**为什么要两处**：LangGraph 的 checkpointer 不知道也不该知道我们的
`graph_version` / `state_schema_version` 兼容规则。M0 终止条件 7 要求
"版本不兼容就走失败终态，不硬恢复"——这个判定必须在恢复**之前**做，因此需要一份
LangGraph 之外的、可查询的版本记录。

恢复流程：

```
读 MySQL 的 checkpoint 记录
  → 校验 graph_version 与 state_schema_version 兼容
      不兼容 → FAILED/version_incompatible，不加载 LangGraph 状态
      兼容   → 用 thread_id 从 AsyncSqliteSaver 恢复
```

`thread_id` 取 `run_id`（M1 的 run_id 形如 `run-<uuid>`，长度 40，远低于 255 上限）。

不选 `PostgresSaver`：项目栈里没有 Postgres（说明书 §11 固定 MySQL），
引入它违反 NG-11。LangGraph 官方无 MySQL checkpointer，因此 SQLite 是唯一
不引入新中间件的持久化选项。**这是一处已知的架构不对称**：业务事实在 MySQL，
Agent 状态在 SQLite 文件。M8 部署时该文件需要持久卷，已记入 §Consequences。

### 4. HITL：用 `interrupt()`，但审批的权威判定仍在 Java

`interrupt(value)` 让图在节点内暂停并把 `value` 交给外部；恢复时用
`Command(resume=...)` 传回结果。

我们的用法：

```
POLICY_CHECK 节点判定需要审批
  → 调 Java 的 POST /api/v1/approvals 创建审批记录（拿到 approval_id）
  → interrupt({"approval_id": ...})  让图暂停
  → 图的状态被 checkpointer 持久化
  ...（人工在控制台批准/拒绝）...
  → 外部轮询到审批已决策
  → Command(resume={"decision": "APPROVED", "approval_id": ...}) 恢复
  → EXECUTING_TOOL 节点执行前调 POST /api/v1/approvals/{id}/consume 做四项比对
```

**关键约束（M0 INV-4）**：`Command(resume=...)` 里携带的 decision **不是**授权凭据，
只是"外部说审批有结果了"这一事件。真正的放行判定在 `consume` 那一步——它在 Java 侧
重新比对 tool_name / resource_ref / arguments_digest / 未过期未消费。

**为什么不能信 resume 里的 decision**：resume 是从我们自己的进程传进来的，
一个 bug 或一次误操作就能构造出 `{"decision": "APPROVED"}`。审批的权威只能在
持有业务事实的那一侧。

### 5. 不用 `interrupt_before` / `interrupt_after`

`compile()` 支持这两个参数，但它们是"在某个节点前后无条件暂停"，是调试设施。
我们的审批是**条件性**的（只有 Policy 判定需要审批时才暂停），因此用节点内的
`interrupt()` 函数式调用。

### 6. 不引入 LangChain 的 LLM 抽象

LangGraph 节点里直接用 M3 的 `ChatProvider`，不套 `langchain_core` 的
`BaseChatModel`。理由：M3 已经把失败语义做成了类型化异常并绑定 `failureClass`，
再套一层会把这些异常包成框架异常，`failure_class` 就丢了。

代价：用不上 LangChain 生态的现成集成。这与 NG-11（不引入第二个 Agent 框架）
的取向一致——我们要的是 LangGraph 的编排与持久化，不是它的全栈。

## Consequences

**正面**：

- 状态机与终止条件不依赖框架，换框架不用重写业务判定。
- 两条执行路径（手写 / LangGraph）的一致性测试使"框架做了什么"有可执行答案。
- 审批的权威判定留在 Java，`interrupt`/`resume` 只承担"暂停与唤醒"。
- 失败语义从 M3 直通到 M6 的归因统计，没有被框架异常吞掉。

**负面 / 代价**：

- **架构不对称**：业务事实在 MySQL，Agent 检查点在 SQLite 文件。M8 部署需要为该文件
  提供持久卷，且多副本部署时同一个 run 必须落到同一副本（或改用共享存储）。
  这是引入 Postgres 之外的唯一选择，属于被 NG-11 约束后的结果。
- 重复实现了 LangGraph 已有的部分能力（步数上限）。
- 不用 LangChain 抽象意味着未来接入新 Provider 要自己写 Adapter。
- `langgraph` 1.2.11 是当日最新版，**API 仍可能变**。本 ADR 的核验结论只对该版本有效，
  依赖已锁定确切版本号。

## 验证方式

1. 8 条终止条件在手写循环上各有单测。
2. 手写循环与 LangGraph 图在同一输入下终态与 failureClass 一致。
3. `AWAITING_APPROVAL` 状态跨进程重启后可恢复（M4 Gate 硬条件）。
4. checkpoint 的 `graph_version` 不兼容时落 `FAILED/version_incompatible`，
   且**未加载** LangGraph 状态。
5. `Command(resume={"decision":"APPROVED"})` 但 Java 侧审批实际是 PENDING/REJECTED
   → 执行仍被拒绝（证明 resume 不是授权凭据）。
