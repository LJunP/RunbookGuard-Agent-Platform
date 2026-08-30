# ADR-0010：动作沙箱的隔离边界与 K8s 部署边界

- 状态：已接受
- 日期：2026-08-30
- 相关：M0 §9（Sandbox 分级）、说明书 §20、M8

## 背景

M0 §9 要求：**任何动作型工具必须进独立进程或容器**，且非 root、只读文件系统、
精确挂载、CPU/内存/PID 限制、默认禁公网、支持明确取消、结果大小受限。

M4 到 M7 期间动作在同进程内执行。当时的说法是「实际隔离来自作用对象本身——
synthetic-lab 的动作只改内存状态」。这个说法**避开了要求本身**：
M0 §9 约束的是执行者的能力边界，不是被作用对象的危险程度。
一个能在 Agent Runtime 进程里跑任意代码的动作路径，即使今天只连 synthetic-lab，
它的能力边界也是「整个 Agent Runtime 进程能做的一切」，包括读到模型凭据。

## 决策

分两层落地，每层解决 M0 §9 的不同条目，**不假装某一层覆盖了全部**。

### 第一层：受限子进程（`tools/action_sandbox.py` + `tools/action_runner.py`）

| M0 §9 要求 | 状态 | 实现 |
|---|---|---|
| 独立进程或容器 | **已实现** | `asyncio.create_subprocess_exec` + `start_new_session=True` |
| 非 root | **已实现** | 启动前检查 `os.geteuid() != 0`，是 root 就拒绝执行 |
| CPU 限制 | **已实现** | `RLIMIT_CPU=10s` |
| 内存限制 | **平台相关** | `RLIMIT_AS=512MiB`；macOS 拒绝设置该项，Linux 可以 |
| PID 限制 | **已实现** | `RLIMIT_NPROC=32` |
| 明确取消 | **已实现** | 超时后 `killpg(SIGKILL)`，强制而非协作式 |
| 结果大小受限 | **已实现** | 父子两侧各有上限（64 KiB / 32 KiB） |
| 禁止写文件 | **已实现** | `RLIMIT_FSIZE=0` + 一次性 cwd |
| 精确挂载 | **未实现** | 子进程与父进程共享文件系统视图。需要容器或 mount namespace |
| 只读根文件系统 | **未实现** | 同上 |
| 默认禁公网 | **应用层已实现，内核层未实现** | 父进程的 `allowed_base_urls` 白名单；内核层见第二层 |

三个设计决定值得单独说明：

**`sandbox=True` 是默认值。** 一个「默认不隔离、需要显式打开」的沙箱等于没有沙箱——
漏掉一处就是一个可执行的缺口。`sandbox=False` 只在单元测试里出现
（respx 的 mock 到不了子进程）。

**限制在子进程里施加并回报，父进程校验。** 最初用 `preexec_fn`，
但它抛异常时只会得到一句不带原因的 `SubprocessError`
（实测：macOS 上 `RLIMIT_AS` 失败时看不出是哪一项）。改成子进程自己设、
把成功的项列在响应里，父进程用 `REQUIRED_LIMITS` 校验。
必需项缺失即判 `sandbox_failure`——一个没有资源上限的「沙箱」看起来像隔离的，
因此比明确的同进程执行更危险。

`RLIMIT_AS` 刻意**不在** `REQUIRED_LIMITS` 里：macOS 上设不上是平台事实，
而 K8s 里真正的内存上限由 cgroup（容器 memory limit）提供，那一层比 rlimit 更可靠。

**子进程的环境是白名单，不是继承。** 父进程里有 `RUNBOOKGUARD_LLM_API_KEY`，
继承过去等于把模型凭据交给一个执行外部动作的进程（M0 INV-6）。
用白名单而非黑名单，是因为新增的敏感变量不需要逐个记得排除。

**runner 不导入任何业务模块。** `import agent_runtime.app` 会连带初始化
Provider 与指标注册表，那些在一个只需要发 HTTP 请求的进程里既无用又扩大攻击面。
代价是三个动作到端点的映射在 runner 里写死了一份——新增动作必须同时改契约与它，
那正好是一次显式的审查点。

### 第二层：K8s NetworkPolicy（`deploy/k8s/base/30-networkpolicy.yaml`）

内核层的 egress 阻断由 NetworkPolicy 提供，不由应用代码提供。
理由是应用层的白名单只能约束「代码走的那条路」，而一个被攻破的进程可以直接建 socket。

前提是 CNI 真的执行策略。**实测结论**（`scripts/verify-networkpolicy-enforcement.sh`）：

```
检测到的 CNI Pod：kindnet-qkhpd
镜像：docker.io/kindest/kindnetd:v20250512-df8de77b
基线：HTTP 200
施加 deny-all ingress 后：HTTP 000
结论：NetworkPolicy 被执行。
```

kindnetd 的 README 只列了三项职责（IP masquerade、netlink 路由、写 CNI 配置），
**既没说支持也没说不支持** NetworkPolicy。因此必须实测：
「文档没提」既不等于支持也不等于不支持。日志里可见它在 `Syncing nftables rules`。

因为策略确实生效，**不换 Calico**。kind 文档把 `disableDefaultCNI` 标注为
"power user feature with limited support"，在策略已能执行的前提下换掉它
只会引入一个不必要的变量。

M8 演练实测 `agent-runtime` 访问 `1.1.1.1:443` 被拒，访问 `synthetic-lab:8090` 通。

## 后果

- 动作执行多了一次进程启动（约 100~300ms）。动作是低频的人工审批后操作，
  这个开销可以接受；只读工具仍在进程内，不受影响。
- `test_action_sandbox.py` 的 25 个测试跑真实子进程，耗时约 47s。
  用 mock 验证进程隔离等于用文档验证文档，这个成本必须付。
- **挂载隔离与只读根文件系统仍是缺口。** 补它们需要把动作放进独立容器
  （Job / sidecar / 外部 runner 服务），那是一次架构变更而不是配置变更。
  在 Gate 报告里必须标为未实现，不能因为「进程隔离已做」就当整条要求满足。
- `sandbox=False` 这条路径的存在本身是风险：它只该出现在测试里。
  `test_sandbox_is_on_by_default` 断言默认值，但没有机制阻止有人在生产代码里传 False。

## 未采用的方案

**Docker-in-Docker 或 K8s Job 每个动作起一个容器。** 隔离更彻底，
但引入了「Agent Runtime 需要 Docker socket 或 K8s RBAC 写权限」这个新的授权面——
为了限制动作而给执行者更大的权限，方向是反的。
真实生产做法应当是一个独立的 action-runner 服务，Agent Runtime 只能向它发请求，
它自己在受限容器里执行。那是 M8 之后的事。

**gVisor / seccomp profile。** 收益明确但依赖宿主内核与运行时配置，
在「一台干净机器照 README 就能跑」这个约束下不可行。
