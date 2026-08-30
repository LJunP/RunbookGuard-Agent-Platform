"""动作工具的进程隔离（M0 §9 Sandbox 分级）。

M0 §9 要求：任何动作型工具必须进独立进程或容器，且非 root、只读文件系统、
精确挂载、CPU/内存/PID 限制、默认禁公网、支持明确取消、结果大小受限。

**这个模块实现的是「独立进程」那一档，不是「容器」那一档。** 逐条对照：

| M0 §9 要求 | 本模块 | 由谁保证 |
|---|---|---|
| 独立进程或容器 | 是，`asyncio.create_subprocess_exec` | 本模块 |
| 非 root | 是，启动前检查 euid != 0 并拒绝 | 本模块 |
| CPU 限制 | 是，`RLIMIT_CPU` | 本模块 |
| 内存限制 | 是，`RLIMIT_AS` | 本模块 |
| PID 限制 | 是，`RLIMIT_NPROC` | 本模块 |
| 精确挂载 | **否**。子进程与父进程共享文件系统视图 | 需要容器 |
| 只读文件系统 | **部分**。cwd 限制在一次性临时目录，但 `/` 仍可读写 | 需要容器 |
| 默认禁公网 | **部分**。runner 只接受一个 allowlist 内的 base URL；内核层没有阻断 | K8s NetworkPolicy（deploy/k8s/base/30-networkpolicy.yaml） |
| 明确取消 | 是，超时后 SIGKILL（强制取消，非协作式） | 本模块 |
| 结果大小受限 | 是，读满上限即截断并判失败 | 本模块 |

「部分」与「否」的三项必须靠容器或内核提供，进程级隔离做不到——
写成「已完成」就是假的。M8 的 K8s 部署里 NetworkPolicy 补上了 egress 那一项
（演练实测 agent-runtime 访问 1.1.1.1 被拒）。挂载与只读根仍是缺口。
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# runner 子进程的入口。用 -m 而不是路径：路径会随打包方式变化，
# 模块名不会。
RUNNER_MODULE = "agent_runtime.tools.action_runner"

# 资源上限。取值偏紧：动作只是发一个 HTTP 请求，不需要更多。
# 偏紧的另一个理由是，上限太松等于没有上限——一个失控的动作仍能拖垮宿主。
CPU_SECONDS = 10
ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
MAX_PROCESSES = 32
MAX_OUTPUT_BYTES = 64 * 1024
# 墙上时钟超时。必须大于 CPU 限制（进程可能在等 IO 而不烧 CPU），
# 且要留出解释器启动时间。
WALL_CLOCK_SECONDS = 20.0


class SandboxError(RuntimeError):
    """沙箱本身的问题（无法启动、被拒绝、超时被杀）。

    与动作自身的失败区分开：后者由 runner 以结构化 JSON 返回，
    前者是隔离机制没能建立起来，两者的处置完全不同。
    """

    def __init__(self, message: str, *, failure_code: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code


@dataclass(frozen=True)
class SandboxResult:
    payload: dict[str, Any]
    stderr: str
    # 子进程 pid。审计需要它来对应 OS 层的记录。
    pid: int
    # 实际施加成功的限制。写进结果而不只是断言，使 Gate 报告能引用
    # 「这次运行真的设上了哪些」而不是「代码里写了哪些」。
    limits_applied: frozenset[str] = frozenset()


# 必须成功施加的资源限制。子进程会报告它实际设上了哪些，父进程据此校验——
# 「以为设上了」与「真的设上了」必须能区分。
#
# RLIMIT_AS 不在必需集里：macOS 上 setrlimit(RLIMIT_AS) 直接抛
# 「current limit exceeds maximum limit」，而这是平台事实不是代码缺陷。
# Linux（K8s 实际运行的地方）上它能设上，因此子进程会照设并报告成功，
# 父进程只是不把它当成硬性前提。真正的内存上限在 K8s 里由 cgroup
# （容器的 memory limit）提供，那一层比 rlimit 更可靠。
REQUIRED_LIMITS = frozenset({"RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NPROC"})

# 子进程要尝试施加的全部限制。值传给子进程而不是在子进程里写死，
# 使「上限是多少」这个决定留在父进程（策略侧）。
LIMIT_SPEC: dict[str, int] = {
    "RLIMIT_CPU": CPU_SECONDS,
    "RLIMIT_AS": ADDRESS_SPACE_BYTES,
    "RLIMIT_NPROC": MAX_PROCESSES,
    # 不允许写文件。动作的产出只走 stdout。
    "RLIMIT_FSIZE": 0,
    # 不产生 core dump：崩溃时的内存镜像可能含凭据。
    "RLIMIT_CORE": 0,
}


def _child_env(*, lab_base_url: str) -> dict[str, str]:
    """子进程的环境变量白名单。

    **不继承父进程的环境**。父进程里有 `RUNBOOKGUARD_LLM_API_KEY`
    这类凭据，继承过去等于把它们交给一个执行外部动作的进程
    （M0 INV-6：Secret 绝不进入不需要它的地方）。

    PATH 与 PYTHONPATH 是启动解释器必需的；HOME 指向一次性目录。
    """
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "RUNBOOKGUARD_ACTION_LAB_URL": lab_base_url,
        # 限制由子进程自己在跑任何业务代码之前施加，并在响应里报告结果。
        #
        # 不用 preexec_fn：它在 fork 之后 exec 之前跑，任何异常都变成一句
        # 不带原因的 SubprocessError（实测：macOS 上 RLIMIT_AS 失败时
        # 只看到 "Exception occurred in preexec_fn"，看不出是哪一项）。
        # 让子进程报告「设上了哪些」还有一个额外好处：父进程可以**校验**，
        # 而不是假设。
        "RUNBOOKGUARD_ACTION_RLIMITS": json.dumps(LIMIT_SPEC),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
    }


async def run_action_in_subprocess(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    idempotency_key: str,
    lab_base_url: str,
    allowed_base_urls: frozenset[str],
    timeout_seconds: float = WALL_CLOCK_SECONDS,
) -> SandboxResult:
    """在受限子进程里执行一个动作。

    egress allowlist 在这里检查而不是在子进程里：子进程的代码是可信的，
    但「它被允许连到哪里」是父进程的策略决定。把判断放进子进程等于让
    被约束者自己决定约束。
    """
    if lab_base_url not in allowed_base_urls:
        raise SandboxError(
            f"{lab_base_url} is not in the egress allowlist {sorted(allowed_base_urls)}",
            failure_code="sandbox_failure",
        )
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # 非 root 是 M0 §9 的硬要求。以 root 跑动作会让所有其它限制失去意义。
        raise SandboxError(
            "refusing to run an action tool as root", failure_code="sandbox_failure"
        )

    request = json.dumps(
        {
            "tool_name": tool_name,
            "arguments": arguments,
            "idempotency_key": idempotency_key,
        },
        ensure_ascii=False,
    )

    # 一次性 cwd。RLIMIT_FSIZE=0 已经禁止写文件，这里再限制目录是纵深防御：
    # 万一某条路径绕过了 rlimit，落点也在一个即将被删除的目录里。
    workdir = tempfile.mkdtemp(prefix="rg-action-")
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",  # 隔离模式：忽略 PYTHON* 环境变量之外的用户 site-packages
                "-m",
                RUNNER_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=_child_env(lab_base_url=lab_base_url),
                # 新会话：子进程成为自己进程组的组长。超时后可以整组 kill，
                # 否则它 fork 出来的孙进程会漏掉。
                start_new_session=True,
            )
        except OSError as exc:
            raise SandboxError(
                f"could not start the action sandbox: {exc}", failure_code="sandbox_failure"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(request.encode()), timeout=timeout_seconds
            )
        except TimeoutError as exc:
            # 强制取消：SIGKILL 整个进程组。协作式取消在这里不够——
            # 一个卡在系统调用里的进程不会响应 SIGTERM，而动作的副作用
            # 可能已经发生，必须尽快停止它继续做别的事。
            _kill_group(process)
            await _reap(process)
            raise SandboxError(
                f"action exceeded {timeout_seconds}s and was killed; "
                "the effect is UNKNOWN, check the action ledger",
                failure_code="tool_timeout",
            ) from exc

        stderr_text = stderr.decode("utf-8", "replace")[:2000]

        if len(stdout) > MAX_OUTPUT_BYTES:
            raise SandboxError(
                f"action produced {len(stdout)} bytes, over the {MAX_OUTPUT_BYTES} limit",
                failure_code="result_too_large",
            )
        if process.returncode != 0:
            raise SandboxError(
                f"action sandbox exited with {process.returncode}: {stderr_text[:300]}",
                failure_code="sandbox_failure",
            )
        try:
            body = json.loads(stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SandboxError(
                f"action sandbox produced non-JSON output: {stdout[:200]!r}",
                failure_code="sandbox_failure",
            ) from exc

        if not isinstance(body, dict):
            raise SandboxError(
                "action sandbox output is not a JSON object",
                failure_code="sandbox_failure",
            )
        if body.get("ok") is not True:
            # runner 报告的业务失败。原样带出 failure_code，
            # 不在这里重新分类——分类是 runner 与契约的职责。
            raise SandboxError(
                str(body.get("message", "action failed")),
                failure_code=str(body.get("failure_code", "sandbox_failure")),
            )
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise SandboxError(
                "action sandbox returned no payload object",
                failure_code="sandbox_failure",
            )

        # 校验限制真的设上了。缺一项就判失败——一个没有资源上限的"沙箱"
        # 与直接在同进程里执行没有区别，而它看起来像是隔离的，更危险。
        applied = frozenset(body.get("limits_applied") or ())
        missing = REQUIRED_LIMITS - applied
        if missing:
            raise SandboxError(
                f"sandbox did not apply required limits {sorted(missing)}; "
                f"applied={sorted(applied)}",
                failure_code="sandbox_failure",
            )
        return SandboxResult(
            payload=payload,
            stderr=stderr_text,
            pid=process.pid or -1,
            limits_applied=applied,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        # setsid 之后子进程是自己进程组的组长，pid 即 pgid。
        os.killpg(os.getpgid(process.pid), 9)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def _reap(process: asyncio.subprocess.Process) -> None:
    """回收已 kill 的子进程。

    不 wait 会留下僵尸进程，而僵尸占 PID——足够多之后连不上新进程，
    症状看起来像「系统资源耗尽」而不是「有个地方漏了 wait」。
    """
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        pass


def runner_module_path() -> Path:
    """runner 模块的实际文件位置。用于诊断「模块找不到」这类问题。"""
    return Path(__file__).with_name("action_runner.py")
