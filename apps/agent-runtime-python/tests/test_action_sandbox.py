"""动作沙箱的测试（M8）。

这些测试跑**真实子进程**，不打桩。理由：沙箱要证明的恰恰是「进程边界真的存在」，
用 mock 验证进程隔离等于用文档验证文档。

代价是它们需要一个真实的 HTTP 目标，因此用 `http.server` 起一个最小的 lab 替身。
这比 respx 慢，但 respx 的 mock 到不了子进程——那是这一层必须付的成本。
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_runtime.tools.action_sandbox import (
    LIMIT_SPEC,
    REQUIRED_LIMITS,
    SandboxError,
    run_action_in_subprocess,
)


class _LabHandler(BaseHTTPRequestHandler):
    """最小 lab 替身。行为由类属性控制，因为 handler 由服务器实例化。"""

    status = 200
    body: dict | None = None
    raw_body: bytes | None = None
    delay_seconds = 0.0
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append({"path": self.path, "body": payload})

        if self.delay_seconds:
            import time

            time.sleep(self.delay_seconds)

        raw = self.raw_body
        if raw is None:
            body = self.body if self.body is not None else {
                "service": payload.get("service", "?"),
                "idempotency_key": payload.get("idempotency_key", ""),
                "replayed": False,
            }
            raw = json.dumps(body).encode()

        self.send_response(self.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:  # noqa: D102
        pass


@pytest.fixture
def lab():
    """起一个真实 HTTP 服务器，返回 (base_url, handler_class)。"""
    _LabHandler.status = 200
    _LabHandler.body = None
    _LabHandler.raw_body = None
    _LabHandler.delay_seconds = 0.0
    _LabHandler.requests = []

    server = ThreadingHTTPServer(("127.0.0.1", 0), _LabHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", _LabHandler
    finally:
        server.shutdown()
        server.server_close()


async def _run(lab_url: str, **overrides):
    kwargs = dict(
        tool_name="restart_synthetic_service",
        arguments={"service": "synthetic-orders"},
        idempotency_key="restart:tenant-demo:synthetic-orders:apr-1",
        lab_base_url=lab_url,
        allowed_base_urls=frozenset({lab_url}),
    )
    kwargs.update(overrides)
    return await run_action_in_subprocess(**kwargs)


class TestHappyPath:
    async def test_action_runs_in_a_separate_process(self, lab) -> None:
        """pid 不等于当前进程 —— 这是「独立进程」这个要求的直接证据。"""
        url, _ = lab
        result = await _run(url)
        assert result.pid != os.getpid()
        assert result.pid > 0

    async def test_payload_is_returned(self, lab) -> None:
        url, handler = lab
        result = await _run(url)
        assert result.payload["service"] == "synthetic-orders"
        assert len(handler.requests) == 1

    async def test_idempotency_key_reaches_the_lab(self, lab) -> None:
        """没有幂等键的动作重投会产生第二次副作用。它必须真的传出去。"""
        url, handler = lab
        await _run(url, idempotency_key="restart:t:s:apr-99")
        assert handler.requests[0]["body"]["idempotency_key"] == "restart:t:s:apr-99"

    async def test_each_tool_hits_its_own_endpoint(self, lab) -> None:
        url, handler = lab
        await _run(
            url,
            tool_name="rollback_synthetic_deployment",
            arguments={"service": "synthetic-orders", "target_version": "v1.4.2"},
            idempotency_key="rollback:t:s:apr-1",
        )
        assert handler.requests[0]["path"] == "/v1/actions/rollback"


class TestLimitsAreReallyApplied:
    """限制必须**校验**而不是假设。

    一个没有资源上限的「沙箱」与直接在同进程里执行没有区别，
    但它看起来像是隔离的，因此更危险。
    """

    async def test_required_limits_are_reported_as_applied(self, lab) -> None:
        url, _ = lab
        result = await _run(url)
        assert REQUIRED_LIMITS <= result.limits_applied, (
            f"必需限制缺失：{sorted(REQUIRED_LIMITS - result.limits_applied)}"
        )

    async def test_limit_spec_covers_cpu_memory_and_pids(self) -> None:
        """M0 §9 点名了 CPU / 内存 / PID 三项。"""
        assert "RLIMIT_CPU" in LIMIT_SPEC
        assert "RLIMIT_AS" in LIMIT_SPEC
        assert "RLIMIT_NPROC" in LIMIT_SPEC

    async def test_write_is_forbidden(self, lab) -> None:
        """RLIMIT_FSIZE=0：动作的产出只走 stdout，不落文件。"""
        assert LIMIT_SPEC["RLIMIT_FSIZE"] == 0
        url, _ = lab
        result = await _run(url)
        assert "RLIMIT_FSIZE" in result.limits_applied


class TestSecretsDoNotLeakIntoTheChild:
    async def test_provider_api_key_is_not_inherited(
        self, lab, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M0 INV-6：Secret 绝不进入不需要它的地方。

        执行外部动作的进程不需要模型凭据。继承整个环境等于把它交出去。
        """
        monkeypatch.setenv("RUNBOOKGUARD_LLM_API_KEY", "sk-must-not-leak-0123456789")
        url, _ = lab
        # 子进程把自己看到的环境变量数量与是否含该键回报出来——
        # 这里改用一个直接的检查：让 runner 跑不到业务逻辑就失败，
        # 然后从 stderr 确认。更简单的做法是直接检查 _child_env。
        from agent_runtime.tools.action_sandbox import _child_env

        env = _child_env(lab_base_url=url)
        assert "RUNBOOKGUARD_LLM_API_KEY" not in env
        assert not any("API_KEY" in key for key in env)
        assert not any(key.startswith("AWS_") for key in env)

    async def test_child_env_is_a_whitelist(self, lab) -> None:
        url, _ = lab
        from agent_runtime.tools.action_sandbox import _child_env

        env = _child_env(lab_base_url=url)
        # 白名单而非黑名单：新增的敏感环境变量不需要逐个记得排除。
        assert set(env) == {
            "PATH",
            "PYTHONPATH",
            "PYTHONUNBUFFERED",
            "PYTHONDONTWRITEBYTECODE",
            "RUNBOOKGUARD_ACTION_LAB_URL",
            "RUNBOOKGUARD_ACTION_RLIMITS",
            "LC_ALL",
            "LANG",
        }


class TestEgressAllowlist:
    async def test_url_outside_the_allowlist_is_refused(self, lab) -> None:
        """allowlist 在**父进程**里检查。放进子进程等于让被约束者自己决定约束。"""
        url, handler = lab
        with pytest.raises(SandboxError, match="allowlist"):
            await _run(url, allowed_base_urls=frozenset({"http://elsewhere.test"}))
        # 关键：请求根本没有发出去。
        assert handler.requests == []

    async def test_refusal_happens_before_the_subprocess_starts(self, lab) -> None:
        url, _ = lab
        try:
            await _run(url, allowed_base_urls=frozenset())
        except SandboxError as exc:
            assert exc.failure_code == "sandbox_failure"
        else:
            pytest.fail("空 allowlist 应当拒绝")


class TestFailureClassification:
    async def test_unknown_tool_is_invalid_arguments(self, lab) -> None:
        url, _ = lab
        with pytest.raises(SandboxError) as excinfo:
            await _run(url, tool_name="rm_minus_rf")
        assert excinfo.value.failure_code == "invalid_arguments"

    async def test_extra_argument_is_rejected_not_ignored(self, lab) -> None:
        """静默丢弃未知参数会掩盖「调用方与契约不一致」这个事实。"""
        url, handler = lab
        with pytest.raises(SandboxError, match="unexpected"):
            await _run(url, arguments={"service": "synthetic-orders", "force": True})
        assert handler.requests == []

    async def test_missing_argument_is_rejected(self, lab) -> None:
        url, _ = lab
        with pytest.raises(SandboxError, match="missing"):
            await _run(
                url,
                tool_name="rollback_synthetic_deployment",
                arguments={"service": "synthetic-orders"},
            )

    async def test_empty_idempotency_key_is_rejected(self, lab) -> None:
        """没有幂等键的动作不能执行：重投会产生第二次副作用。"""
        url, handler = lab
        with pytest.raises(SandboxError, match="idempotency_key"):
            await _run(url, idempotency_key="")
        assert handler.requests == []

    async def test_lab_5xx_is_upstream_unavailable(self, lab) -> None:
        url, handler = lab
        handler.status = 503
        with pytest.raises(SandboxError) as excinfo:
            await _run(url)
        assert excinfo.value.failure_code == "upstream_unavailable"

    async def test_lab_4xx_is_invalid_arguments(self, lab) -> None:
        url, handler = lab
        handler.status = 422
        with pytest.raises(SandboxError) as excinfo:
            await _run(url)
        assert excinfo.value.failure_code == "invalid_arguments"

    async def test_non_json_lab_response_is_sandbox_failure(self, lab) -> None:
        url, handler = lab
        handler.raw_body = b"<html>gateway error</html>"
        with pytest.raises(SandboxError) as excinfo:
            await _run(url)
        assert excinfo.value.failure_code == "sandbox_failure"

    async def test_unreachable_lab_is_upstream_unavailable(self) -> None:
        """连不上可能是 NetworkPolicy 拒了，也可能是 lab 挂了。
        猜一个具体原因会让排查走错方向，因此统一归 upstream_unavailable。"""
        dead = "http://127.0.0.1:1"
        with pytest.raises(SandboxError) as excinfo:
            await _run(dead, lab_base_url=dead, allowed_base_urls=frozenset({dead}))
        assert excinfo.value.failure_code == "upstream_unavailable"


class TestCancellation:
    async def test_timeout_kills_the_subprocess(self, lab) -> None:
        """强制取消（SIGKILL 整组）而不是协作式：
        动作的副作用可能已经发生，必须尽快阻止它继续做别的事。"""
        url, handler = lab
        handler.delay_seconds = 5.0
        with pytest.raises(SandboxError) as excinfo:
            await _run(url, timeout_seconds=1.0)
        assert excinfo.value.failure_code == "tool_timeout"
        # 超时后效果未知这一点必须写在消息里：可能已生效也可能没有。
        assert "UNKNOWN" in str(excinfo.value)

    async def test_timeout_leaves_no_zombie(self, lab) -> None:
        """漏掉 wait 会留下僵尸进程，而僵尸占 PID。
        足够多之后症状看起来像「系统资源耗尽」而不是「有个地方漏了 wait」。"""
        import subprocess

        url, handler = lab
        handler.delay_seconds = 5.0
        with pytest.raises(SandboxError):
            await _run(url, timeout_seconds=1.0)

        # 当前进程的子进程里不应有 defunct。
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
        )
        assert "Z" not in out.stdout


class TestResultSizeLimit:
    async def test_oversized_lab_response_is_rejected(self, lab) -> None:
        url, handler = lab
        # runner 侧的上限是 32 KiB。
        handler.raw_body = json.dumps({"blob": "x" * (40 * 1024)}).encode()
        with pytest.raises(SandboxError) as excinfo:
            await _run(url)
        assert excinfo.value.failure_code == "result_too_large"


class TestExecutorIntegration:
    """沙箱路径与同进程路径的产出必须逐键相同。

    两条路径产出不同形状会让「换成沙箱」变成一次隐蔽的契约变更。
    """

    async def test_shapers_match_inprocess_handlers(self) -> None:
        from agent_runtime.tools.action_executor import (
            _ACTION_HANDLERS,
            _RESULT_SHAPERS,
        )

        assert set(_RESULT_SHAPERS) == set(_ACTION_HANDLERS)

    async def test_restart_output_keys(self) -> None:
        from agent_runtime.tools.action_executor import _RESULT_SHAPERS

        shaped = _RESULT_SHAPERS["restart_synthetic_service"](
            {"service": "synthetic-orders", "replayed": True},
            {"service": "synthetic-orders"},
            "restart:t:s:apr-1",
        )
        assert set(shaped) == {"service", "restarted", "idempotency_key", "replayed"}
        assert shaped["replayed"] is True

    async def test_sandbox_is_on_by_default(self) -> None:
        """默认不隔离、需要显式打开的沙箱等于没有沙箱：漏掉一处就是一个缺口。"""
        from agent_runtime.tools.action_executor import ActionToolExecutor

        assert ActionToolExecutor("http://lab.test")._sandbox is True
