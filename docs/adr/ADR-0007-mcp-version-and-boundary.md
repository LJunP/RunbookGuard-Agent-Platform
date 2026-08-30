# ADR-0007：MCP 协议与 SDK 版本锁定，以及 MCP 的业务边界

- Status: Accepted
- Date: 2026-08-27
- 里程碑：M4（M3 Gate 报告 §6.1 的阻塞项 B3）
- 关联：[M0 §8 MCP 边界](../architecture/M0-product-brief.md)、[威胁模型 T-4](../architecture/M0-threat-model.md)

## Context

说明书 §15 与 DEV_PROMPT §8 都要求：动手前核验当日 MCP 正式版本、SDK 支持情况和迁移
说明，结果写进 ADR。这不是形式要求——MCP 的 Python SDK 刚经历了一次破坏性大版本升级。

## 核验结果（2026-08-27 实测）

### 协议版本

`https://modelcontextprotocol.io/specification/` 当前指向的 schema 路径是
`schema/2026-07-28/schema.ts`，页面内所有子章节链接均为 `/specification/2026-07-28/*`。

已安装 SDK 中的常量（`mcp.types`，实测）：

```
LATEST_PROTOCOL_VERSION      = '2026-07-28'
DEFAULT_NEGOTIATED_VERSION   = '2025-03-26'
JSONRPC_VERSION              = '2.0'
PROTOCOL_VERSION_META_KEY    = 'io.modelcontextprotocol/protocolVersion'
UNSUPPORTED_PROTOCOL_VERSION = -32022
```

**注意 `LATEST` 与 `DEFAULT_NEGOTIATED` 不同**：SDK 默认协商到 `2025-03-26`（更保守），
而最新规范是 `2026-07-28`。这意味着"用了最新 SDK"不等于"跑在最新协议上"。
manifest 里必须分别记录这两个值。

### SDK：`mcp` 2.x 是破坏性变更

已安装 `mcp==2.1.1`（当日 PyPI 最新）。实测的迁移信息由 SDK 自身在导入失败时给出：

```
>>> from mcp.server.fastmcp import FastMCP
ModuleNotFoundError: No module named 'mcp.server.fastmcp'.
This is mcp 2.x, where FastMCP was renamed to MCPServer
(from mcp.server.mcpserver import MCPServer) and other APIs changed;
see the migration guide at
https://py.sdk.modelcontextprotocol.io/v2/migration/#fastmcp-renamed-to-mcpserver
or pin 'mcp<2' to keep running v1 code.
```

**这正是铁律五存在的理由**：凭记忆写 `from mcp.server.fastmcp import FastMCP`
（1.x 的写法，也是绝大多数现存教程的写法）在当日最新 SDK 上直接 ImportError。

实测可用的导入路径：

```
from mcp.server import Server              -> OK（lowlevel）
from mcp import ClientSession              -> OK
from mcp.client.session import ClientSession -> OK
from mcp.client.stdio import stdio_client  -> OK
from mcp import stdio_server               -> OK
from mcp import Tool, CallToolRequest, ListToolsResult, ServerCapabilities  -> OK
```

`mcp.server.fastmcp` 在 2.x 中**不存在**。

### 协议层面的新增（与本项目相关的）

规范页列出的客户端能力里出现了 **Elicitation**（服务端主动向用户索取信息），
以及 **Tasks** 扩展（长任务异步执行、轮询、mid-flight input、durable handles）。

## Decision

### 1. 锁定 `mcp==2.1.1`，使用 lowlevel `Server`，不用 `MCPServer`

原因不是 `MCPServer` 不好，而是 lowlevel `Server` 让每个请求的处理路径显式可见。
本项目的核心主张是"工具调用的每一层都可被审查"，一个自动从函数签名推导 schema 的
装饰器 API 会把 schema 生成藏起来——而工具 Contract 的 11 项（说明书 §14）需要逐项
显式声明，其中 principal/tenant/resource binding、风险等级、幂等键都不是函数签名
能表达的。

代价：代码更长。收益：`tools/list` 返回的每个字段都是我写的，不是框架推导的。

### 2. Manifest 必须同时记录协议版本与 SDK 版本

`contracts/tools/mcp-manifest.json` 记录：

```
sdk_package / sdk_version                 = mcp / 2.1.1
sdk_latest_protocol_version               = 2026-07-28
sdk_default_negotiated_version            = 2025-03-26
spec_revision_verified                    = 2026-07-28
verified_at                               = 2026-08-27
```

只记 SDK 版本不够：同一个 SDK 能说多个协议版本。只记协议版本也不够：同一协议在不同
SDK 版本上的实现差异会导致行为不同。M6 冻结评测时要冻结的是"tool versions"，
这两个都属于它。

### 3. MCP 是可替换的传输层，direct adapter 是主路径

说明书 §15 要求"同时提供 direct adapter，避免把 MCP 当业务核心"。落地方式：

```
ToolRegistry（工具的唯一定义点：schema + 11 项 Contract）
   ├── DirectToolTransport   进程内直接调用     ← 默认，CI 与评测用它
   └── McpToolTransport      经 MCP Server 调用  ← 证明协议兼容性
```

两个 transport 必须通过**同一份契约测试**：同一工具名 + 同一参数，两条路径返回同构
结果。这条测试防止 MCP 路径悄悄漂移出独立行为。

评测默认走 direct：MCP 引入子进程与 JSON-RPC 往返，会给 M6 的可复现性增加不受控变量。

### 4. MCP transport 鉴权不替代业务 RBAC（威胁 T-4）

这是最容易做错的一条，说明书 §15 专门点出来了。

即使 MCP 连接已建立且经过 transport 层鉴权，工具执行前仍必须在业务层重新校验：
工具名在 allowlist 内、tenant 与 resource binding 来自 Control Plane 的权威数据、
参数通过 schema 校验。

**具体实现约束**：MCP Server 进程**不持有**任何授权判定逻辑。它收到 `tools/call`
后把请求交给同一个 `PolicyEngine`（与 direct 路径共用），由后者裁决。
MCP Server 只是一个协议翻译层。

**tenant 绝不从 MCP 请求参数读取**。它从 Control Plane 派生。客户端自报的 tenant
是不可信输入——这与 M1 的 `AuthenticatedCallerArgumentResolver` 同一个道理。

### 5. 只读 MCP Server，不暴露动作工具

M4 只把 5 个只读工具经 MCP 暴露。三个动作工具（restart / rollback / throttle）
**不进** MCP Server。

理由：动作工具需要审批 + 参数摘要绑定 + 隔离执行。让它们走一条额外的协议路径，
就多了一处需要证明"审批没被绕过"的地方，而这个收益是零——审批的权威在 Java，
MCP 在这里不提供任何东西。

### 6. 不启用 Elicitation，不启用 Tasks 扩展

- **Elicitation**（服务端向用户索取信息）：它让 MCP Server 获得了向人提问的通道。
  在本项目的信任模型里，Server 是受控代码但不是权威——它不该有独立的人机交互通道。
  人机交互只有一条路径：Java Control Plane 的 Approval。
- **Tasks**（长任务异步执行）：我们已经有 Lease + Checkpoint + 幂等的完整方案
  （M2/ADR-0003）。再引入一套 durable handle 会出现两套恢复语义，
  "唯一终态"就有两个裁决点。

两者都在初始化时不声明对应 capability。

### 7. 不把任何第三方 MCP Server 视为可信

本项目只连自己实现的 Server。若未来接入第三方：
它返回的 tool description 与 annotation 均视为不可信数据（规范页自己也这么写：
"descriptions of tool behavior such as annotations should be considered untrusted"），
且必须经过同一个 PolicyEngine。**第一版不接任何第三方 Server。**

## Consequences

**正面**：

- 版本核验暴露了一个会直接导致 ImportError 的记忆错误（`mcp.server.fastmcp`），
  以及一个更隐蔽的问题（LATEST ≠ DEFAULT_NEGOTIATED）。
- direct adapter 为主路径使评测不受 MCP 往返的干扰。
- 两个 transport 的契约测试使"MCP 是可替换传输层"这句话可验证。
- 授权判定只有一个实现（PolicyEngine），MCP 路径不可能绕过它。

**负面 / 代价**：

- lowlevel `Server` 代码量大于 `MCPServer` 装饰器 API。
- 锁定 `mcp==2.1.1`：该 SDK 刚做过破坏性升级，短期内可能继续变动。
  升级前必须重跑两个 transport 的契约测试。
- 默认协商版本 `2025-03-26` 落后于最新规范 `2026-07-28`。**不主动提升**：
  提升需要验证新协议版本下的行为差异，而这对本项目要证明的东西没有增量价值。
  这一点在 manifest 中如实记录，不声称"支持最新协议"。
- 不启用 Tasks 意味着 MCP 侧的长任务能力用不上。我们的长任务靠 Lease/Checkpoint。

## 验证方式

1. MCP Server 的 `tools/list` 返回 5 个只读工具，且每个工具的 schema 与
   `ToolRegistry` 中的定义逐字段一致。
2. `tools/list` **不含**任何动作工具。
3. direct 与 MCP 两条路径对同一工具调用返回同构结果（契约测试）。
4. 经 MCP 调用一个 allowlist 外的工具 → PolicyEngine 拒绝，且产生审计事件。
5. MCP 请求参数里塞 `tenant_id` → 被忽略，实际使用 Control Plane 派生的 tenant。
6. manifest 中记录的 SDK 版本与运行时 `mcp.__version__` 一致（防止 manifest 过期）。
7. 初始化时不声明 elicitation / tasks capability。
