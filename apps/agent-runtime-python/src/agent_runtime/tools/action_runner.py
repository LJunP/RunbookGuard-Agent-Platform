"""动作沙箱的子进程入口。

从 stdin 读一个 JSON 请求，把 HTTP 调用发给 synthetic-lab，向 stdout 写一个
JSON 响应。**不做任何策略判断**：授权与 allowlist 都在父进程里决定了，
在这里重判等于让被约束者自己决定约束。

它刻意很短。这个进程是隔离边界的内侧，代码越少越容易论证「它只能做这一件事」：
  - 只读 stdin，只写 stdout
  - 只连一个从环境变量取来的 base URL
  - 不导入 agent_runtime 的任何业务模块（避免把 Provider、Policy 之类一并拉进来）

不导入业务模块这一点是刻意的：`import agent_runtime.app` 会连带初始化 Provider
与指标注册表，那些东西在一个只需要发 HTTP 请求的进程里既无用又扩大了攻击面。
"""

from __future__ import annotations

import json
import os
import resource
import sys
from typing import Any
from urllib import error, request

# 动作到 lab 端点的映射。写死而不是从契约里读：契约模块属于业务代码，
# 导入它会把整棵依赖树拉进这个进程。三个动作是封闭集合（M0 §14），
# 因此写死不会漂移——新增动作必须同时改契约与这里，那正好是一次显式的审查点。
_ENDPOINTS = {
    "restart_synthetic_service": "/v1/actions/restart",
    "rollback_synthetic_deployment": "/v1/actions/rollback",
    "throttle_synthetic_traffic": "/v1/actions/throttle",
}

# 每个动作允许的参数键。多余的键直接拒绝而不是忽略：
# 一个带着未知参数的动作说明调用方与契约不一致，静默丢弃会掩盖那个不一致。
_ALLOWED_ARGS = {
    "restart_synthetic_service": {"service"},
    "rollback_synthetic_deployment": {"service", "target_version"},
    "throttle_synthetic_traffic": {"service", "rate_basis_points"},
}

HTTP_TIMEOUT_SECONDS = 8.0
MAX_RESPONSE_BYTES = 32 * 1024


def _fail(failure_code: str, message: str, limits: list[str] | None = None) -> int:
    json.dump(
        {
            "ok": False,
            "failure_code": failure_code,
            "message": message,
            "limits_applied": limits or [],
        },
        sys.stdout,
        ensure_ascii=False,
    )
    sys.stdout.flush()
    # 退出码 0：失败信息已经在 stdout 里结构化表达了。
    # 非零退出码会让父进程把它当成「沙箱本身坏了」，而这是动作的业务失败。
    return 0


def _apply_rlimits() -> list[str]:
    """施加父进程指定的资源上限，返回真正设上的那些。

    在**任何**业务代码之前调用。返回而不是抛异常，是为了让父进程能区分
    「这一项在这个平台上设不上」与「沙箱整体失败」——前者是平台事实
    （例如 macOS 拒绝 setrlimit(RLIMIT_AS)），后者才是缺陷。
    父进程持有必需项清单并据此判定。
    """
    spec = os.environ.get("RUNBOOKGUARD_ACTION_RLIMITS", "")
    if not spec:
        return []
    try:
        limits = json.loads(spec)
    except ValueError:
        return []
    applied: list[str] = []
    for name, value in sorted(limits.items()):
        target = getattr(resource, name, None)
        if target is None:
            continue
        try:
            resource.setrlimit(target, (int(value), int(value)))
        except (ValueError, OSError):
            # 设不上就不记。父进程会因为必需项缺失而拒绝这次执行。
            continue
        applied.append(name)
    return applied


def main() -> int:
    limits_applied = _apply_rlimits()
    base_url = os.environ.get("RUNBOOKGUARD_ACTION_LAB_URL", "").rstrip("/")
    if not base_url:
        return _fail("sandbox_failure", "RUNBOOKGUARD_ACTION_LAB_URL is not set")

    raw = sys.stdin.read()
    try:
        req = json.loads(raw)
    except ValueError as exc:
        return _fail("invalid_arguments", f"request is not valid JSON: {exc}")
    if not isinstance(req, dict):
        return _fail("invalid_arguments", "request is not a JSON object")

    tool_name = req.get("tool_name")
    arguments = req.get("arguments")
    idempotency_key = req.get("idempotency_key")

    if tool_name not in _ENDPOINTS:
        return _fail("invalid_arguments", f"unknown action tool {tool_name!r}")
    if not isinstance(arguments, dict):
        return _fail("invalid_arguments", "arguments must be an object")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        # 没有幂等键的动作不能执行：重投会产生第二次副作用。
        return _fail("invalid_arguments", "idempotency_key is required")

    extra = set(arguments) - _ALLOWED_ARGS[tool_name]
    if extra:
        return _fail("invalid_arguments", f"unexpected arguments {sorted(extra)}")
    missing = _ALLOWED_ARGS[tool_name] - set(arguments)
    if missing:
        return _fail("invalid_arguments", f"missing arguments {sorted(missing)}")

    body: dict[str, Any] = {"idempotency_key": idempotency_key, **arguments}
    url = f"{base_url}{_ENDPOINTS[tool_name]}"

    payload = json.dumps(body, ensure_ascii=False).encode()
    http_req = request.Request(
        url,
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(http_req, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw_body = response.read(MAX_RESPONSE_BYTES + 1)
    except error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace")
        if exc.code >= 500:
            return _fail("upstream_unavailable", f"lab returned {exc.code}: {detail[:300]}")
        return _fail("invalid_arguments", f"lab returned {exc.code}: {detail[:300]}")
    except error.URLError as exc:
        # 连不上可能是 egress 被 NetworkPolicy 拒了，也可能是 lab 挂了。
        # 两者都归 upstream_unavailable：从这个进程里区分不出来，
        # 猜一个具体原因会让排查走错方向。
        return _fail("upstream_unavailable", f"cannot reach {url}: {exc.reason}")
    except TimeoutError:
        return _fail("tool_timeout", f"lab did not respond within {HTTP_TIMEOUT_SECONDS}s")

    if len(raw_body) > MAX_RESPONSE_BYTES:
        return _fail("result_too_large", f"lab response exceeds {MAX_RESPONSE_BYTES} bytes")

    try:
        lab_body = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return _fail("sandbox_failure", f"lab returned non-JSON: {exc}")
    if not isinstance(lab_body, dict):
        return _fail("sandbox_failure", "lab returned a non-object body")

    json.dump(
        {"ok": True, "payload": lab_body, "limits_applied": limits_applied},
        sys.stdout,
        ensure_ascii=False,
    )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
