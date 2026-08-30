"""只读 MCP Server（ADR-0007）。

用 lowlevel `mcp.server.Server` 而非 2.x 的 `MCPServer` 装饰器 API：工具 Contract 的
11 项里有 principal/tenant/resource binding、风险等级、幂等键，这些不是函数签名能
表达的，必须显式声明。代价是代码更长，收益是 tools/list 返回的每个字段都是我写的。

**Server 进程不持有任何授权判定逻辑。** 它收到 tools/call 后交给与 direct 路径共用的
同一个 PolicyEngine。tenant 从注入的上下文取，绝不从请求参数读——客户端自报的
tenant 是不可信输入（威胁 T-4）。

只暴露 5 个只读工具。三个动作工具不进 MCP：让它们走一条额外的协议路径，就多了一处
需要证明「审批没被绕过」的地方，而 MCP 在这里不提供任何增量。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..tools.catalogue import READ_ONLY_TOOLS
from ..tools.contract import ResourceBinding, ToolEnvironment, ToolSuggestion
from ..tools.executor import ReadOnlyToolExecutor, ToolFailure
from ..tools.policy import PolicyEngine, PolicyInput

MCP_SERVER_NAME = "runbookguard-readonly-tools"
MCP_SERVER_VERSION = "0.1.0"


@dataclass(frozen=True)
class McpCallContext:
    """每次 MCP 会话的授权上下文。

    由启动 Server 的进程注入，不来自 MCP 请求——这是 tenant 不可被客户端自报的
    实现方式。一个 Server 进程服务一个 (principal, tenant) 组合。
    """

    principal_id: str
    tenant_id: str
    permitted_resources: frozenset[str]
    allowed_tool_names: frozenset[str]
    environment: ToolEnvironment = ToolEnvironment.SYNTHETIC_LAB


def tool_descriptors() -> list[dict[str, Any]]:
    """tools/list 的返回内容。

    每个字段显式写出，包括 MCP 协议本身不要求的风险等级与审批要求——
    客户端据此能判断「这个工具会不会改变什么」，而不必去读文档。
    """
    return [
        {
            "name": contract.name,
            "description": contract.description,
            "inputSchema": contract.input_schema,
            # MCP 的 annotations 是给客户端的提示。规范明确说它们应被视为不可信
            # （除非来自可信 Server），因此这里的值仅供展示，不作为任何判定依据。
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
            },
            "_meta": {
                "version": contract.version,
                "risk": contract.risk.value,
                "requiresApproval": contract.requires_approval,
                "timeoutSeconds": contract.timeout_seconds,
                "cancelSemantics": contract.cancel_semantics.value,
                "maxResultBytes": contract.max_result_bytes,
                "typedFailures": sorted(contract.typed_failures),
                "allowedEnvironments": sorted(e.value for e in contract.allowed_environments),
            },
        }
        for contract in READ_ONLY_TOOLS
    ]


class McpToolBridge:
    """tools/call 的处理逻辑，与 MCP 传输层解耦。

    拆出来的理由：它要能在没有 MCP 会话的情况下被直接测试，
    而 direct/MCP 两条路径的契约测试需要比较的正是这一层的输出。
    """

    def __init__(
        self,
        *,
        policy: PolicyEngine,
        executor: ReadOnlyToolExecutor,
        context: McpCallContext,
    ) -> None:
        self.policy = policy
        self.executor = executor
        self.context = context
        from ..tools.catalogue import BY_NAME

        self._by_name = BY_NAME

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """返回 {"ok": bool, ...}。

        不抛异常给 MCP 层：MCP 的错误响应会丢失我们的 typed failure 分类，
        而 M6 的失败归因统计需要它。失败作为结构化结果返回，由客户端解释。
        """
        contract = self._by_name.get(tool_name)
        if contract is None:
            return _denied("unknown_tool", f"tool {tool_name!r} is not in the catalogue")

        # 动作工具即使被请求也不在此暴露。双重保险：tools/list 不含它们，
        # 这里再挡一次，防止客户端凭猜测直接调用。
        if contract.is_write():
            return _denied(
                "not_in_allowlist",
                f"{tool_name} is a write tool and is not exposed over MCP",
            )

        resource_ref = _resource_ref(arguments)
        binding = ResourceBinding(
            principal_id=self.context.principal_id,
            tenant_id=self.context.tenant_id,
            resource_ref=resource_ref,
        )

        from ..agent.bounded_loop import _validate_arguments
        from ..approval.digest import digest as digest_fn

        try:
            arguments_digest = digest_fn(arguments)
        except Exception:
            arguments_digest = None

        decision = self.policy.decide(
            PolicyInput(
                suggestion=ToolSuggestion(tool_name=tool_name, arguments=dict(arguments)),
                contract=contract,
                binding=binding,
                environment=self.context.environment,
                allowed_tool_names=self.context.allowed_tool_names,
                permitted_resources=self.context.permitted_resources,
                arguments_digest=arguments_digest,
                schema_errors=_validate_arguments(contract, arguments),
                # MCP 只暴露只读工具，因此不涉及审批。
                approval=None,
                tool_call_budget=0,
            )
        )
        if not decision.allowed:
            return _denied(
                decision.deny_reason.value if decision.deny_reason else "not_in_allowlist",
                decision.reason,
            )

        try:
            result = await self.executor.execute(decision.authorization)
        except ToolFailure as failure:
            failure.validate_against(contract)
            return {
                "ok": False,
                "failure_code": failure.failure_code,
                "message": str(failure),
            }
        return {
            "ok": True,
            "tool_name": result.tool_name,
            "payload": result.payload,
            "untrusted": result.untrusted,
        }


def _denied(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "failure_code": code, "message": message, "denied_by_policy": True}


def _resource_ref(arguments: dict[str, Any]) -> str:
    if "service" in arguments:
        return f"svc:{arguments['service']}"
    if "queue" in arguments:
        return f"queue:{arguments['queue']}"
    return "runbook:*"


def build_server(bridge: McpToolBridge):
    """构造 MCP Server。

    延迟导入 mcp：它是可替换的传输层（ADR-0007 §3），direct 路径不该因为
    MCP SDK 不可用而无法工作。

    **注意 2.x 的 API 形状**：装饰器式的 `@server.list_tools()` 在 1.x 才有；
    2.x 改成构造期注入 `on_list_tools` / `on_call_tool` 回调，且字段名从
    camelCase 改成 snake_case（`input_schema` 而非 `inputSchema`）。
    这两点是实测得到的（ADR-0007），不是从文档推测的。
    """
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

    async def on_list_tools(context, params) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(
                    name=d["name"],
                    description=d["description"],
                    input_schema=d["inputSchema"],
                    # meta 承载 MCP 协议不要求但工具契约需要的字段：
                    # 风险等级、审批要求、typed failure 清单。
                    meta=d["_meta"],
                )
                for d in tool_descriptors()
            ]
        )

    async def on_call_tool(context, params) -> CallToolResult:
        outcome = await bridge.call(params.name, dict(params.arguments or {}))
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(outcome, ensure_ascii=False))],
            # is_error 让客户端不必解析 payload 就知道调用失败。
            # 但失败**原因**仍在 payload 的 failure_code 里——MCP 的错误通道
            # 会丢失我们的 typed failure 分类，而 M6 的归因统计需要它。
            is_error=not outcome.get("ok", False),
        )

    return Server(
        MCP_SERVER_NAME,
        version=MCP_SERVER_VERSION,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve_stdio(bridge: McpToolBridge) -> None:
    from mcp import stdio_server

    server = build_server(bridge)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
