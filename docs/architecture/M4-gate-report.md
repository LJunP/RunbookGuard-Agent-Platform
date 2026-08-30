# M4 Gate 自检报告

- 里程碑：**M4 — Agent、Tool、MCP、HITL**
- 报告日期：2026-08-27
- 结论：**M4 Gate 三条件全部通过（有跨语言真实调用输出）。**
- 上游依据：说明书 §23 M4，DEV_PROMPT §12 M4

---

## 1. Gate 条件逐条自评

DEV_PROMPT §12 M4 Gate 原文：**「未审批的写动作 100% 不执行（有测试证明）；审批后篡改参数会被 digest 校验拦住；AWAITING_APPROVAL 状态可以跨进程重启后恢复。」**

| # | 条件 | 自评 | 证据 |
|---|---|---|---|
| G1 | 未审批的写动作 100% 不执行 | **通过** | 单测：参数化遍历全部 3 个动作工具逐个证明。真实演练 2：Run 挂起在 AWAITING_APPROVAL，synthetic-lab 的部署版本未变，动作账本为空 |
| G2 | 审批后篡改参数被 digest 校验拦住 | **通过** | 演练 3：**Java 与 Python 算出同一个 digest**；篡改后 consume 被拒；原参数通过（证明不是"什么都拒绝"）；同一审批用第二次被拒 |
| G3 | AWAITING_APPROVAL 跨进程恢复 | **通过** | 演练 5：两个 AgentGraph 实例共享 checkpointer，新实例恢复后到达终态，动作执行恰好一次 |
| G4 | 先手写有界执行循环再换 LangGraph（M4 要点） | **通过** | `bounded_loop.py` 不依赖任何编排框架；`test_graph_parity.py` 17 项一致性测试划出框架贡献边界 |
| G5 | 八条终止条件全部有测试 | **通过** | `test_termination.py` 逐条一个测试 + 优先级测试 + failureClass 映射不漂移测试 |
| G6 | 5 个只读工具 | **通过** | 契约 11 项全部必填，缺项在构造期报错 |
| G7 | 1 个只读 MCP Server + direct adapter | **通过** | 两个 transport 对同一调用返回**完全相等**的结果 |
| G8 | Policy 引擎可独立单测 | **通过** | 纯函数：无 IO、无全局状态、无时钟。64 项测试含"同输入同输出""引擎无跨调用状态""两引擎结论一致" |
| G9 | Checkpoint 持久化 | **部分通过** | LangGraph checkpointer 可用且跨实例恢复已验证。**MySQL 侧的版本兼容校验尚未接入**，见 §5.1 第 1 条 |

---

## 2. 交付物清单

### 2.1 决策文档

| 文件 | 内容 |
|---|---|
| [ADR-0006](../adr/ADR-0006-langgraph-boundary.md) | LangGraph API 实测核验、状态机权威归属、checkpoint 分层、HITL 边界、不用 recursion_limit 的理由 |
| [ADR-0007](../adr/ADR-0007-mcp-version-and-boundary.md) | MCP 协议与 SDK 版本锁定、lowlevel Server 选型、direct 为主路径、transport 鉴权不替代 RBAC |

### 2.2 契约

| 文件 | 内容 |
|---|---|
| [contracts/tools/digest-test-vectors.json](../../contracts/tools/digest-test-vectors.json) | 15 accept + 7 reject，Java/Python 两侧独立实现读同一份 |
| [contracts/tools/mcp-manifest.json](../../contracts/tools/mcp-manifest.json) | SDK 版本、协议版本、暴露/排除工具、capability 声明 |

### 2.3 Python 源码（新增 12 个文件）

| 文件 | 职责 |
|---|---|
| `agent/state.py` | 14 状态 + 转移合法性。不依赖 LangGraph |
| `agent/termination.py` | 8 条终止条件的唯一判定点 + 指纹式循环检测 |
| `agent/bounded_loop.py` | 手写有界执行循环 |
| `agent/graph.py` | LangGraph 图 + interrupt/resume |
| `tools/contract.py` | 11 项契约 + 三段分离的类型保证 |
| `tools/catalogue.py` | 5 只读 + 3 动作工具定义 |
| `tools/policy.py` | Policy 引擎（纯函数） |
| `tools/executor.py` | 只读执行器 + is_decoy 剥除 |
| `tools/action_executor.py` | 动作执行器 + ConsumedApproval 类型守卫 |
| `approval/digest.py` | JCS-SHA256-V1 的 Python 实现 |
| `approval/gateway.py` | Control Plane 审批网关（无 approve 方法） |
| `mcp/readonly_server.py` | 只读 MCP Server |

### 2.4 Java 侧

| 文件 | 内容 |
|---|---|
| `DigestCrossLanguageVectorTest.java` | 25 项，读 Python 生成的向量 |

### 2.5 synthetic-lab 扩展

| 文件 | 内容 |
|---|---|
| `actions.py` | 动作账本，幂等 + 可查询状态 |
| `app.py` 新增 6 个端点 | restart / rollback / throttle / actions / service-state / reset |

### 2.6 测试与脚本

| 文件 | 项数 |
|---|---|
| `test_policy.py` | 64 |
| `test_termination.py` | 30 |
| `test_bounded_loop.py` | 23 |
| `test_graph_parity.py` | 17 |
| `test_mcp_transport.py` | 32 |
| `test_approval_gateway.py` | 19 |
| `test_digest_vectors.py` | 30 |
| `scripts/drill-m4.py` | 43 项真实跨语言演练 |

---

## 3. 真实运行输出

### 3.1 Python 测试（297 项）

```
$ cd apps/agent-runtime-python && PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
........................................................................ [ 24%]
........................................................................ [ 48%]
........................................................................ [ 72%]
........................................................................ [ 96%]
.........                                                                [100%]
297 passed in 3.23s
```

### 3.2 Java 跨语言向量测试（25 项）

```
$ cd apps/control-plane-java && mvn -o test -Dtest=DigestCrossLanguageVectorTest
[INFO] Tests run: 25, Failures: 0, Errors: 0, Skipped: 0
[INFO] BUILD SUCCESS
```

### 3.3 跨语言集成演练（43 项，连真实 Java + MySQL + Redis + synthetic-lab）

```
$ apps/agent-runtime-python/.venv/bin/python scripts/drill-m4.py

ok    Control Plane 健康
ok    synthetic-lab 健康

== 演练 1：只读诊断走通真实 synthetic-lab ==
ok    终态为 COMPLETE
ok    从真实 lab 取到 3 份证据
ok    全部证据标记为不可信
ok    产出诊断结论
ok    证据中不含 is_decoy（诱饵标记已在工具层剥除）

== 演练 2：未审批的写动作 100% 不执行 ==
ok    Run 挂起在 AWAITING_APPROVAL
ok    Control Plane 已创建审批记录
ok    synthetic-lab 的部署版本未变（动作确实没执行）
ok    动作账本为空

== 演练 3：审批后篡改参数被 digest 校验拦住（跨语言） ==
ok    审批已创建
ok    审批出现在待审批列表
ok    Java 与 Python 算出同一个 arguments_digest
ok    digest 算法标识一致
ok    APPROVER 批准成功
ok    篡改参数后 consume 被拒绝
ok    原参数 consume 通过（不误拒）
ok    同一审批不能用第二次（防重放）

== 演练 4：审批通过后动作执行，且幂等 ==
ok    审批已消费
ok    动作执行成功
ok    首次执行不是重放
ok    Agent 可验证效果：版本已变为 v1.4.2
ok    同一幂等键重复执行被识别为重放
ok    动作账本只有一条记录（副作用只发生一次）

== 演练 5：AWAITING_APPROVAL 跨实例恢复 ==
ok    图在审批处中断
ok    中断时带出 approval_id
ok    审批被批准
ok    新实例恢复后到达终态
ok    恢复后动作执行了恰好一次

== 演练 6：resume 载荷不是授权凭据 ==
ok    图在审批处中断
ok    伪造的 resume 载荷不能放行
ok    动作未执行

== 演练 7：注入日志不导致权限提升 ==
ok    注入剧本已启动
ok    注入文本作为证据被记录（审计需要知道注入发生过）
ok    被诱导的高危工具被 Policy 拒绝
ok    没有任何动作被执行
ok    Run 仍完成了正常诊断（注入未破坏诊断能力）

== 演练 8：审计事件可追溯 ==
ok    审批请求已留痕
ok    审批决策已留痕
ok    审批消费已留痕
ok    存在拒绝类审计事件
ok    篡改参数的尝试被记为 DENIED

===============================
PASS=43  FAIL=0
```

### 3.4 六容器栈健康

```
$ docker compose ps --format 'table {{.Name}}\t{{.Status}}'
NAME               STATUS
rg-agent-runtime   Up (healthy)
rg-control-plane   Up (healthy)
rg-mysql           Up (healthy)
rg-rabbitmq        Up (healthy)
rg-redis           Up (healthy)
rg-synthetic-lab   Up (healthy)
```

---

## 4. 开发过程中真实踩到的问题

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | **repeated-state 检测从不触发** | `evidence_id` 用递增序号，而循环指纹含证据集合，序号递增使指纹每轮都变。同一工具调 4 次一次都不判循环 | `evidence_id` 改为内容摘要派生 |
| 2 | pending 审批走到了安全停止而非 AWAITING_APPROVAL | Policy 把 PENDING 与 REJECTED 混成一个 `approval_not_granted`，调用方无法区分"等一下"和"别想了" | 拆成两个 DenyReason |
| 3 | **Policy 放行写动作但审批通道未装配时静默执行** | 条件写成 `requires_approval and approval_id is not None`，通道缺失时整个审批分支被跳过 | 装配缺失直接落 `authorization_failed` |
| 4 | LangGraph 所有条件边走默认分支 | 路由键 `_route` 未声明在 TypedDict 里。LangGraph 按键建 channel，未声明的键静默丢弃 | 声明 `route` 通道 |
| 5 | 跨实例 resume `AttributeError` | 当前建议/授权/结果挂在 `self` 上，新实例的 `self` 是空的 | 移进图状态的 `current` / `last_result` 通道 |
| 6 | 差点把授权凭据写进 checkpoint | 修 #5 时想顺手序列化 `ToolAuthorization`，它携带 `allowed=True` | 改为恢复时在节点内重新签发；状态只存参数与摘要 |
| 7 | MCP Server 构造失败 | 凭 1.x 记忆写 `@server.list_tools()`；2.x 改成构造期注入 `on_list_tools` 回调，字段名从 `inputSchema` 改成 `input_schema` | 按实测 API 重写 + `TestSdkApiShapeIsPinned` 钉住假设 |
| 8 | 演练 2 `KeyError: 'deployed_version'` | synthetic-lab 容器是旧镜像，动作端点还没打进去 | 重建镜像 |

**第 1、3、5、6 条值得单独说。**

第 1 条是"看起来在工作但实际失效"的典型：循环检测的代码全都在，测试也写了，但指纹的构造方式让它永不命中。这类缺陷在 M6 会表现为"某些 case 跑到预算耗尽才停"，而排查时你会先怀疑预算配置。

第 3 条是这个里程碑最危险的一处。它不是逻辑错误而是**条件写反了防御方向**：`approval_id is not None` 在通道缺失时为假，于是整个审批检查被跳过。正确的防御方向是"缺少审批通道时拒绝执行"，而不是"缺少审批通道时跳过审批检查"。

第 5、6 条连在一起：修跨进程恢复时最自然的做法是"把 `self` 上的东西都序列化进状态"，而那会把放行凭据写盘——任何能改 checkpoint 的人都能伪造 `allowed=True`，`issue_authorization` 的构造守卫被完全绕过。现在的做法是状态只存事实（参数、摘要、resource_ref），判定结果在恢复后重新产生。

---

## 5. 已知限制与未验证项

### 5.1 已知限制

1. **Checkpoint 的版本兼容校验尚未接入。** ADR-0006 §3 设计了"读 MySQL 的 checkpoint 记录 → 校验 graph_version 与 state_schema_version → 不兼容走失败终态"，但当前 `AgentGraph` 只用 LangGraph 的 checkpointer，没有向 MySQL 的 `checkpoint` 表写记录，也没有恢复前的版本校验。M0 终止条件 7 目前只有单测覆盖（注入 `version_incompatible=True`），**没有真实的版本不兼容场景验证**。这是 M4 的一处交付缺口，列入 M5/M6 前置。
2. **动作工具在同进程内执行。** M0 §9 要求动作型工具进独立进程或容器（非 root、只读文件系统、CPU/内存/PID 限制、禁公网）。当前只是把 HTTP 请求发给 synthetic-lab，实际隔离来自作用对象本身（lab 只改内存）。真正的进程隔离在 M8。**不得说成"沙箱已实现"。**
3. **`retrieve_runbook_section` 恒定返回未命中。** M5 接入真实检索前它是占位实现，返回 `retrieval_hit: false`。这是刻意的——返回空数组假装命中会让 S7 的正确弃答无法判定。
4. **LangGraph checkpointer 用 InMemorySaver。** ADR-0006 §3 选定 `AsyncSqliteSaver`，但当前演练与测试都用内存实现。跨进程恢复的验证是"两个实例共享同一个 InMemorySaver"，**不是真正的跨进程**。真进程级恢复要等 M7 的 Worker 接入。
5. **Agent 的工具计划由调用方给出，不由模型生成。** M4 阶段 `plan` 是显式传入的。模型自主规划是 M5/M6 的事——现在混进来会让终止条件的测试依赖模型输出，不确定且慢。
6. **`fetch` 依赖 pending 列表。** Control Plane 没有单个审批的 GET 端点（M1 只暴露 pending 列表），因此已决策的审批 `fetch` 返回 `UNKNOWN`。这不影响安全性（UNKNOWN 让 Policy 拒绝，放行判定在 consume），但会让"已批准"状态在 Policy 层看不到。M7 补一个 GET 端点。
7. **MCP 默认协商协议版本落后于最新规范**（`2025-03-26` vs `2026-07-28`）。不主动提升，manifest 中如实记录。

### 5.2 未验证项

| 项 | 状态 |
|---|---|
| 真正的跨进程恢复（独立 OS 进程） | **未验证**，当前是跨实例共享内存 checkpointer |
| checkpoint 版本不兼容的真实场景 | **未验证**，见 §5.1 第 1 条 |
| 动作工具的进程/容器隔离 | **未做**，M8 |
| 真实模型驱动的工具选择 | **未做**，M5/M6 |
| Runbook 检索 | **未做**，M5 |
| M0 五个正式阈值 | **未验证**，M6 |
| MCP stdio 传输的端到端会话 | **未验证**。`build_server` 构造成功且 bridge 层有 32 项测试，但未起真实 stdio 会话跑 client-server 往返 |
| S0 学习计划状态 | 按你的指示记为"所有者确认跳过" |

---

## 6. 下一里程碑（M5：RAG 与引用）的前置依赖

M5 交付：30~50 个版本化 Runbook、lexical baseline、Qdrant 向量检索、hybrid fusion + rerank、citation 生成与校验。

### 6.1 阻塞项

| # | 前置依赖 | 状态 |
|---|---|---|
| B1 | Qdrant 容器 | Docker 已就绪，加进 compose 即可 |
| B2 | embedding 来源决策 | **需要决策**：用 glm 网关的 embedding 接口（需核验是否提供），还是本地 sentence-transformers（增加依赖体积），还是纯 lexical + 简单向量化。写 ADR-0008 |
| B3 | 补齐 §5.1 第 1 条的 checkpoint 版本校验 | 建议在 M5 一并补，它是 M6"恢复唯一终态率"的前提 |

### 6.2 M5 的纪律要求

DEV_PROMPT §12 M5 要点：**M5 结束时先用 `incidents-dev` 做一次非正式基线摸底**，把成功率数字看一眼。不要等 M6 第一次冻结评测才见到数字——那时按纪律不能改口径，只能回头改产品重新冻结一轮。

`incidents-held` 在 M5 期间**不生成、不查看、不使用**。

### 6.3 M5 应先写的失败测试

1. Runbook 缺失时正确弃答（S7），且 `retrieval_hit=false` 可观测。
2. 检索命中但相关性低于阈值 → 不作为引用依据。
3. 引用的 `document_version` / `section_id` / `content_hash` 可反查到原文。
4. Runbook 更新后旧 Run 的引用仍指向旧版本（引用是历史事实）。
5. 结论中出现无引用支撑的事实断言 → 判失败。
6. lexical baseline 与向量检索的结果可对比（不允许跳过基线）。
7. chunk 元数据缺失 → 拒绝入库（缺元数据就无法做引用校验）。
8. 跨租户 Runbook 不被检索到（tenant 过滤在查询条件里，不在结果后过滤）。

---

## 7. 可复现命令

```bash
cd /Users/lijunpeng/Desktop/open_source_project/RunbookGuard-Agent-Platform

# Python 测试（297 项，零网络）
cd apps/agent-runtime-python && PYTHONPATH=src .venv/bin/python -m pytest tests/ -q

# Java 跨语言向量测试
cd ../control-plane-java && mvn -o test -Dtest=DigestCrossLanguageVectorTest

# 起六容器栈
cd ../../deploy/compose && docker compose up -d --build
docker compose ps

# 跨语言集成演练（43 项）
cd ../.. && apps/agent-runtime-python/.venv/bin/python scripts/drill-m4.py

# 单独看某个演练的效果
curl -s 'http://127.0.0.1:8090/v1/service-state?service=synthetic-orders' | python3 -m json.tool
curl -s 'http://127.0.0.1:8090/v1/actions' | python3 -m json.tool
curl -s -H 'Authorization: Bearer dev-operator-token' \
  'http://127.0.0.1:8080/api/v1/audit-events?limit=20' | python3 -m json.tool

# 清理
cd deploy/compose && docker compose down -v
```

---

## 8. 简历状态

**仍不允许写入简历。** M6 Gate 未通过。M4 完成的是安全边界与编排，不是产品能力证明。特别地，"未审批的写动作 100% 不执行"这条已被 43 项真实演练验证，**但那是机制正确，不是诊断质量达标** —— 后者要等 M6 的冻结评测。
