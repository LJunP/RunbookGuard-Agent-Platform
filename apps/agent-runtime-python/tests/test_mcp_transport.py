"""direct 与 MCP 两个 transport 的契约测试（ADR-0007 §3）。

「MCP 是可替换的传输层」这句话在这里变成可验证的：同一工具名 + 同一参数，
两条路径必须返回同构结果。这条测试防止 MCP 路径悄悄漂移出独立行为。

同时验证 ADR-0007 的其它承诺：只暴露 5 个只读工具、动作工具不出现、
tenant 不从请求参数读、授权判定共用同一个 PolicyEngine。
"""

from __future__ import annotations

import importlib.metadata as metadata
import json
from pathlib import Path

import httpx
import pytest
import respx

from agent_runtime.mcp.readonly_server import (
    MCP_SERVER_NAME,
    McpCallContext,
    McpToolBridge,
    build_server,
    tool_descriptors,
)
from agent_runtime.tools import catalogue
from agent_runtime.tools.contract import ResourceBinding, ToolEnvironment, issue_authorization
from agent_runtime.tools.executor import ReadOnlyToolExecutor
from agent_runtime.tools.policy import PolicyEngine

LAB = "http://lab.test"
MANIFEST = Path(__file__).resolve().parents[3] / "contracts" / "tools" / "mcp-manifest.json"

CONTEXT = McpCallContext(
    principal_id="prin-agent",
    tenant_id="tenant-demo",
    permitted_resources=frozenset({"svc:synthetic-orders", "queue:synthetic-notify.work"}),
    allowed_tool_names=frozenset(t.name for t in catalogue.READ_ONLY_TOOLS),
    environment=ToolEnvironment.SYNTHETIC_LAB,
)

METRICS_ARGS = {"service": "synthetic-orders", "metric": "db_pool_active"}


def _bridge(*, context: McpCallContext | None = None) -> McpToolBridge:
    return McpToolBridge(
        policy=PolicyEngine(),
        executor=ReadOnlyToolExecutor(LAB),
        context=context or CONTEXT,
    )


def _mount_lab() -> None:
    respx.get(f"{LAB}/v1/metrics").mock(
        return_value=httpx.Response(
            200,
            json={
                "service": "synthetic-orders",
                "metric": "db_pool_active",
                "unit": "connections",
                "coverage": "covered",
                "points": [{"timestamp": "2026-08-27T11:30:00+00:00", "value": 12.0}],
            },
        )
    )
    respx.get(f"{LAB}/v1/runtime-events").mock(
        return_value=httpx.Response(
            200, json={"service": "synthetic-orders", "coverage": "covered", "events": []}
        )
    )
    respx.get(f"{LAB}/v1/deployments").mock(
        return_value=httpx.Response(
            200,
            json={
                "service": "synthetic-orders",
                "deployments": [
                    {
                        "version": "v1.5.0",
                        "previous_version": "v1.4.2",
                        "deployed_at": "2026-08-27T11:50:00+00:00",
                        "changed_config_keys": ["orders.repository.connectionLeaseMode"],
                        "is_decoy": True,
                    }
                ],
            },
        )
    )


async def _direct_call(tool_name: str, arguments: dict) -> dict:
    """direct transport：Policy 放行后直接调 Executor。"""
    from agent_runtime.agent.bounded_loop import _validate_arguments
    from agent_runtime.approval.digest import digest as digest_fn
    from agent_runtime.tools.contract import ToolSuggestion
    from agent_runtime.tools.policy import PolicyInput

    contract = catalogue.BY_NAME[tool_name]
    resource_ref = (
        f"svc:{arguments['service']}" if "service" in arguments
        else f"queue:{arguments['queue']}" if "queue" in arguments
        else "runbook:*"
    )
    decision = PolicyEngine().decide(
        PolicyInput(
            suggestion=ToolSuggestion(tool_name=tool_name, arguments=dict(arguments)),
            contract=contract,
            binding=ResourceBinding("prin-agent", "tenant-demo", resource_ref),
            environment=ToolEnvironment.SYNTHETIC_LAB,
            allowed_tool_names=CONTEXT.allowed_tool_names,
            permitted_resources=CONTEXT.permitted_resources,
            arguments_digest=digest_fn(arguments),
            schema_errors=_validate_arguments(contract, arguments),
            tool_call_budget=0,
        )
    )
    assert decision.allowed, decision.reason
    result = await ReadOnlyToolExecutor(LAB).execute(decision.authorization)
    return {
        "ok": True,
        "tool_name": result.tool_name,
        "payload": result.payload,
        "untrusted": result.untrusted,
    }


class TestManifestMatchesReality:
    """manifest 过期比没有 manifest 更危险：它会让人相信一个不成立的事实。"""

    def test_manifest_exists(self) -> None:
        assert MANIFEST.exists(), f"missing {MANIFEST}"

    def test_sdk_version_matches_installed(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        assert manifest["sdk"]["version"] == metadata.version("mcp")

    def test_protocol_constants_match_installed_sdk(self) -> None:
        import mcp.types as mcp_types

        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        assert manifest["sdk"]["latest_protocol_version"] == mcp_types.LATEST_PROTOCOL_VERSION
        assert (
            manifest["sdk"]["default_negotiated_version"]
            == mcp_types.DEFAULT_NEGOTIATED_VERSION
        )

    def test_latest_and_negotiated_differ_as_documented(self) -> None:
        """装了最新 SDK 不等于跑在最新协议上。manifest 里记两个值就是为了这个。"""
        import mcp.types as mcp_types

        assert mcp_types.LATEST_PROTOCOL_VERSION != mcp_types.DEFAULT_NEGOTIATED_VERSION

    def test_exposed_tools_match_manifest(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        assert set(manifest["server"]["exposed_tools"]) == {
            d["name"] for d in tool_descriptors()
        }

    def test_excluded_tools_are_action_tools(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        assert set(manifest["server"]["excluded_tools"]) == {
            t.name for t in catalogue.ACTION_TOOLS
        }

    def test_manifest_declares_transport_auth_does_not_replace_rbac(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        assert manifest["security"]["transport_auth_replaces_business_rbac"] is False
        assert manifest["security"]["tenant_from_request_params"] == "ignored"


class TestToolExposure:
    def test_only_five_read_only_tools_are_listed(self) -> None:
        names = {d["name"] for d in tool_descriptors()}
        assert names == {t.name for t in catalogue.READ_ONLY_TOOLS}
        assert len(names) == 5

    def test_no_action_tool_is_listed(self) -> None:
        names = {d["name"] for d in tool_descriptors()}
        for tool in catalogue.ACTION_TOOLS:
            assert tool.name not in names

    def test_descriptor_schema_matches_the_contract(self) -> None:
        """tools/list 返回的 schema 必须是契约里那一份，不是另写一份。"""
        for descriptor in tool_descriptors():
            contract = catalogue.BY_NAME[descriptor["name"]]
            assert descriptor["inputSchema"] == contract.input_schema
            assert descriptor["_meta"]["version"] == contract.version
            assert descriptor["_meta"]["risk"] == contract.risk.value

    def test_descriptor_exposes_typed_failures(self) -> None:
        for descriptor in tool_descriptors():
            contract = catalogue.BY_NAME[descriptor["name"]]
            assert descriptor["_meta"]["typedFailures"] == sorted(contract.typed_failures)

    def test_annotations_are_marked_read_only(self) -> None:
        for descriptor in tool_descriptors():
            assert descriptor["annotations"]["readOnlyHint"] is True
            assert descriptor["annotations"]["destructiveHint"] is False


class TestTransportParity:
    """同一工具 + 同一参数，两条路径返回同构结果。"""

    @respx.mock
    async def test_metrics_parity(self) -> None:
        _mount_lab()
        direct = await _direct_call("get_service_metrics", METRICS_ARGS)
        via_mcp = await _bridge().call("get_service_metrics", METRICS_ARGS)
        assert direct == via_mcp

    @respx.mock
    async def test_deployments_parity_including_decoy_stripping(self) -> None:
        """is_decoy 在两条路径上都被剥除——剥除逻辑在工具层，不在传输层。"""
        _mount_lab()
        args = {"service": "synthetic-orders"}
        direct = await _direct_call("get_recent_deployments", args)
        via_mcp = await _bridge().call("get_recent_deployments", args)
        assert direct == via_mcp
        assert "is_decoy" not in json.dumps(via_mcp)

    @respx.mock
    async def test_both_mark_results_untrusted(self) -> None:
        _mount_lab()
        direct = await _direct_call("get_service_metrics", METRICS_ARGS)
        via_mcp = await _bridge().call("get_service_metrics", METRICS_ARGS)
        assert direct["untrusted"] is via_mcp["untrusted"] is True

    @respx.mock
    async def test_typed_failure_parity(self) -> None:
        """上游失败在两条路径上给出同一个 failure_code。"""
        respx.get(f"{LAB}/v1/metrics").mock(
            return_value=httpx.Response(
                404, json={"error": "unknown_service", "message": "nope"}
            )
        )
        respx.get(f"{LAB}/v1/runtime-events").mock(
            return_value=httpx.Response(200, json={"events": []})
        )
        from agent_runtime.tools.executor import ToolFailure

        via_mcp = await _bridge().call("get_service_metrics", METRICS_ARGS)
        assert via_mcp["ok"] is False
        assert via_mcp["failure_code"] == "unknown_service"

        with pytest.raises(ToolFailure) as exc:
            await _direct_call("get_service_metrics", METRICS_ARGS)
        assert exc.value.failure_code == via_mcp["failure_code"]


class TestMcpDoesNotBypassPolicy:
    """ADR-0007 §4：transport 鉴权不替代业务 RBAC。"""

    @respx.mock
    async def test_action_tool_over_mcp_is_denied(self) -> None:
        """即使客户端凭猜测直接调用，也必须被挡住。"""
        _mount_lab()
        outcome = await _bridge().call(
            "restart_synthetic_service", {"service": "synthetic-orders"}
        )
        assert outcome["ok"] is False
        assert outcome["denied_by_policy"] is True

    @respx.mock
    async def test_unknown_tool_over_mcp_is_denied(self) -> None:
        outcome = await _bridge().call("rm_minus_rf", {})
        assert outcome["ok"] is False
        assert outcome["failure_code"] == "unknown_tool"

    @respx.mock
    async def test_resource_outside_permitted_set_is_denied(self) -> None:
        """跨资源访问（威胁 T-4）在 MCP 路径上同样被拒。"""
        outcome = await _bridge().call(
            "get_service_metrics", {"service": "synthetic-db", "metric": "db_pool_active"}
        )
        assert outcome["ok"] is False
        assert outcome["failure_code"] == "resource_not_permitted"

    @respx.mock
    async def test_tool_outside_allowlist_is_denied(self) -> None:
        narrow = McpCallContext(
            principal_id="prin-agent",
            tenant_id="tenant-demo",
            permitted_resources=CONTEXT.permitted_resources,
            allowed_tool_names=frozenset({"search_service_logs"}),
        )
        outcome = await _bridge(context=narrow).call("get_service_metrics", METRICS_ARGS)
        assert outcome["ok"] is False
        assert outcome["failure_code"] == "not_in_allowlist"

    @respx.mock
    async def test_schema_violation_is_denied(self) -> None:
        outcome = await _bridge().call("get_service_metrics", {"service": "synthetic-orders"})
        assert outcome["ok"] is False
        assert outcome["failure_code"] == "schema_violation"

    @respx.mock
    async def test_unexpected_field_is_denied(self) -> None:
        outcome = await _bridge().call(
            "get_service_metrics", {**METRICS_ARGS, "sudo": True}
        )
        assert outcome["ok"] is False
        assert outcome["failure_code"] == "schema_violation"


class TestTenantIsNotClientControlled:
    """tenant 从注入的上下文取，绝不从请求参数读。"""

    @respx.mock
    async def test_tenant_in_arguments_is_ignored(self) -> None:
        _mount_lab()
        # 客户端试图自报 tenant。它是 schema 外的字段，因此先被 schema 挡住——
        # 这本身就是「参数里的 tenant 不可能生效」的一种保证。
        outcome = await _bridge().call(
            "get_service_metrics", {**METRICS_ARGS, "tenant_id": "tenant-b"}
        )
        assert outcome["ok"] is False
        assert outcome["failure_code"] == "schema_violation"

    @respx.mock
    async def test_context_tenant_is_used_for_binding(self) -> None:
        _mount_lab()
        outcome = await _bridge().call("get_service_metrics", METRICS_ARGS)
        assert outcome["ok"] is True
        # 结果本身不含 tenant——binding 只用于授权判定，不污染工具输出。
        assert "tenant" not in json.dumps(outcome["payload"]).lower()


class TestServerConstruction:
    def test_server_can_be_built(self) -> None:
        """构造成功即证明 lowlevel Server 的装饰器 API 与当前 SDK 兼容。"""
        server = build_server(_bridge())
        assert server.name == MCP_SERVER_NAME

    def test_fastmcp_module_is_absent_in_v2(self) -> None:
        """ADR-0007 记录的破坏性变更：凭记忆写 mcp.server.fastmcp 会 ImportError。
        这条测试把那个事实钉住，防止有人「顺手」改回旧写法。"""
        with pytest.raises(ModuleNotFoundError):
            import mcp.server.fastmcp  # noqa: F401


class TestSdkApiShapeIsPinned:
    """把 ADR-0007 实测到的 2.x API 形状钉住。

    这些不是「测 SDK」，而是测「我们对 SDK 的假设」。SDK 升级后若形状变了，
    这里会失败并指向 ADR-0007 的核验记录，而不是让 Server 在运行时才崩。
    """

    def test_lowlevel_server_takes_callbacks_not_decorators(self) -> None:
        """1.x 的 @server.list_tools() 在 2.x 不存在，改为构造期注入回调。"""
        from mcp.server import Server

        probe = Server("probe")
        assert not hasattr(probe, "list_tools")
        assert not hasattr(probe, "call_tool")

    def test_tool_uses_snake_case_input_schema(self) -> None:
        """字段名从 inputSchema 改成 input_schema。凭记忆写 camelCase 会静默丢 schema。"""
        from mcp.types import Tool

        assert "input_schema" in Tool.model_fields
        assert "inputSchema" not in Tool.model_fields

    def test_call_tool_result_carries_is_error(self) -> None:
        from mcp.types import CallToolResult

        assert "is_error" in CallToolResult.model_fields

    def test_mcpserver_exists_but_we_do_not_use_it(self) -> None:
        """MCPServer 是 FastMCP 的新名字。ADR-0007 §1 选 lowlevel Server 的理由：
        工具契约的 11 项里有 binding、风险等级、幂等键，不是函数签名能表达的。"""
        import mcp.server

        assert hasattr(mcp.server, "MCPServer")


class TestServerCapabilities:
    """ADR-0007 §6：不声明 elicitation 与 tasks。"""

    def test_server_declares_only_tools(self) -> None:
        from mcp.server import NotificationOptions

        server = build_server(_bridge())
        caps = server.get_capabilities(NotificationOptions(), {})
        assert caps.tools is not None
        # elicitation 会给 Server 一条独立的向人提问通道；人机交互只有一条路径：
        # Control Plane 的 Approval。
        assert getattr(caps, "elicitation", None) is None

    def test_server_metadata_matches_manifest(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        server = build_server(_bridge())
        assert server.name == manifest["server"]["name"]
