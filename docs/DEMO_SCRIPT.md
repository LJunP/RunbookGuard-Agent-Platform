# 5 分钟演示脚本

录屏用。全程 **fake provider、零网络、零成本**，不需要任何模型凭据。

**这段演示的主线不是「它能诊断故障」，而是「它被拒绝的时候，拒绝是真的」。**
诊断能力有 HolmesGPT 和 kagent 在做；这里要展示的是权限边界能不能被证明。

---

## 录制前准备（不计入 5 分钟）

栈要**提前起好**。首次构建 5~10 分钟，镜头里不能等。

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build
bash scripts/wait-for-stack.sh
bash scripts/smoke-m7.sh          # 30 项全绿再开录
```

浏览器开三个标签页，按演示顺序排好：

| 标签 | 地址 |
|---|---|
| 1 · 控制台 | http://127.0.0.1:8081 |
| 2 · Grafana | http://127.0.0.1:3000 |
| 3 · 终端 | 留给 curl |

控制台顶部先粘 `dev-viewer-token`（只读视角开场，最不容易误操作）。

**录制前跑一次干净的**：`down -v` 再 `up -d`，避免镜头里出现上次演示的残留数据。

---

## 时间轴

### 0:00 – 0:35 · 它是什么，以及它不是什么

**画面**：控制台首页。

**旁白**：

> 这是 RunbookGuard，一个故障诊断与**受控处置**的 Agent 平台。
> 它读告警、指标、日志和版本化 Runbook，产出带引用的判断。
>
> 但它不是聊天机器人，也不是能任意执行 shell 的全自动运维 Agent。
> 这个项目真正的内核是一句话：**如何让一个会调用工具的 AI 在运维场景里不出事，并且能被证明不出事。**
>
> 接下来五分钟，我不演示它诊断得多准。我演示它**被拦住的时候，拦截是不是真的**。

---

### 0:35 – 1:15 · 造一个 Incident

**画面**：切终端。

```bash
export CP=http://127.0.0.1:8080

INCIDENT_ID=$(curl -s -X POST \
  -H "Authorization: Bearer dev-operator-token" \
  -H 'Content-Type: application/json' \
  -d '{"source":"synthetic-lab","severity":"P1","title":"synthetic-orders 连接池耗尽"}' \
  "$CP/api/v1/incidents" | python3 -c 'import json,sys;print(json.load(sys.stdin)["incidentId"])')

echo "$INCIDENT_ID"
```

**旁白**：

> 用 OPERATOR 的 token 建一个 P1 故障：synthetic-orders 连接池耗尽。
>
> 注意这一步用的是 operator，不是 viewer。**同样这条请求换成 `dev-viewer-token`，返回 403** ——
> 权限不是前端藏个按钮，是服务端的事。

**可选补一刀**（5 秒，效果很好）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer dev-viewer-token" \
  -H 'Content-Type: application/json' \
  -d '{"source":"synthetic-lab","severity":"P2","title":"x"}' \
  "$CP/api/v1/incidents"
# 403
```

---

### 1:15 – 2:25 · Trace 与引用

**画面**：切回控制台，「Incident 与 Run」页，选中刚才那条 Run。

**旁白**：

> 这是这次诊断的完整 Trace：每一步的节点名、状态、耗时、预算消耗。
>
> 重点看**证据**这一栏。每条结论都挂着 `evidence_id`，每个 id 能反查到
> 它来自哪个工具、哪个服务、哪个版本、内容哈希是多少。
>
> Runbook 引用也一样——36 个版本化 Runbook，引用落到**章节级**，
> 带 `content_hash`。冻结评测里 citation validity 是 1.0，意思是
> 每一条引用都能反查回去，没有一条是模型编的。
>
> 还有一条不在画面上但值得说：日志、指标、工具返回、Runbook 内容，
> 全部被划为**不可信数据**，不能改变控制流。
> 日志里出现 "ignore previous instructions" 是数据，不是指令——
> 评测集里有 7 个 case 专门检验这一点。

---

### 2:25 – 3:35 · 核心：agent token 尝试审批 → 403

**画面**：切终端。这是整段演示的**高潮**，节奏放慢。

```bash
# 0) 先拿到 RUN_ID。上一段是界面操作，没有产出 shell 变量；
#    直接用 $RUN_ID 会是空值，审批请求会 400。
RUN_ID=$(curl -s -X POST \
  -H "Authorization: Bearer dev-agent-token" \
  -H 'Content-Type: application/json' \
  -d "{\"incidentId\":\"$INCIDENT_ID\",\"graphVersion\":\"langgraph-v1\",\"promptVersion\":\"p1\",\"modelId\":\"fake-model\",\"datasetVersion\":\"incidents-dev\"}" \
  "$CP/api/v1/runs" | python3 -c 'import json,sys;print(json.load(sys.stdin)["runId"])')

# 1) Agent 发起一个写动作的审批请求：回滚 synthetic-orders
APPROVAL_ID=$(curl -s -X POST \
  -H "Authorization: Bearer dev-agent-token" \
  -H 'Content-Type: application/json' \
  -d "{\"runId\":\"$RUN_ID\",\"toolName\":\"rollback_synthetic_deployment\",\"resourceRef\":\"svc:synthetic-orders\",\"arguments\":{\"service\":\"synthetic-orders\",\"target_version\":\"v1.4.2\"}}" \
  "$CP/api/v1/approvals" | python3 -c 'import json,sys;print(json.load(sys.stdin)["approvalId"])')

# 2) 同一个 agent token，试图批准自己发起的审批
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer dev-agent-token" \
  -H 'Content-Type: application/json' \
  -d '{"approve":true,"reason":"self"}' \
  "$CP/api/v1/approvals/$APPROVAL_ID/decision"
# 403
```

**旁白**（这段逐字念，别即兴）：

> Agent 发起了一个写动作：回滚 synthetic-orders 到 v1.4.2。
> 这是需要人工审批的动作。
>
> 现在我用**同一个 agent token**，去批准它自己发起的这条审批。
>
> 【停顿，等 403 出现在屏幕上】
>
> 403。
>
> 这里要说清楚它为什么是 403，因为「返回了 403」和「不可能不返回 403」
> 是完全不同的两件事。
>
> **第一，Approval 的权威判定只在 Java 控制面。**
> Python 运行时里根本不存在 `approve` 这个方法——
> 你可以现在 grep 整个 agent-runtime，`def approve` 零命中。
> 不是「Python 侧调用前检查了权限」，是**没有那条代码路径**。
>
> **第二，就算绕过审批，也执行不了。**
> 模型输出的只是 ToolCall *建议*；Policy 在服务端*校验*；Executor 才*执行*。
> 三段不在同一个函数里。
> 而且 `ToolAuthorization(allowed=True)` 只能由 `issue_authorization()` 构造，
> 后者被 `_POLICY_ISSUER_TOKEN` 保护——
> **绕过 Policy 直接造一个「已授权」出来，在类型系统层面就不成立。**

**再补一刀**（参数篡改，15 秒）：

```bash
# APPROVER 正常批准
curl -s -X POST -H "Authorization: Bearer dev-approver-token" \
  -H 'Content-Type: application/json' -d '{"approve":true,"reason":"demo"}' \
  "$CP/api/v1/approvals/$APPROVAL_ID/decision"

# 批准之后，把 target_version 偷偷改掉再去 consume
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer dev-agent-token" \
  -H 'Content-Type: application/json' \
  -d '{"toolName":"rollback_synthetic_deployment","resourceRef":"svc:synthetic-orders","arguments":{"service":"synthetic-orders","target_version":"v0.0.1"}}' \
  "$CP/api/v1/approvals/$APPROVAL_ID/consume"
# 403
```

> 审批通过了，但我把回滚目标从 v1.4.2 换成 v0.0.1 再去执行。
> 还是 403。审批绑定的是**参数摘要**（`arguments_digest`，JCS-SHA256-V1），
> 执行前重新比对审批 id、工具名、资源、参数摘要四项，任一不符就拒绝。
> 「批了 A 执行 B」这条路是堵死的。

---

### 3:35 – 4:30 · 审计：DENIED 记录

**画面**：切控制台「审计事件」页。筛 `DENIED`。

**旁白**：

> 刚才那两次 403，都在这里。
>
> **被拒绝的尝试比允许的更重要。**
>
> 一个只记录成功操作的审计日志，回答不了事后最该问的那个问题：
> 「有没有什么东西，试过做它不该做的事？」
>
> 这些记录不会因为回滚而消失，也不会因为 Run 失败而被清掉。
> 谁、在哪条 Run 上、对哪个资源、想执行哪个工具、被哪条规则拦下——
> 全部落在 Java 控制面的审计表里。

---

### 4:30 – 5:00 · 收尾：那个我自己推翻的指标

**画面**：控制台或 README 的评测表。

**旁白**：

> 最后 30 秒，说一件不在演示里但更重要的事。
>
> 冻结评测里有个指标叫「安全红线拒绝率」，六轮都是 1.0000。
>
> 写简历前我做最后一轮复查，发现**这个 1.0 全是平凡真**。
> 统计「已执行工具」的字段 `tool_name` 恒为 `None`，
> 「已执行工具」这个集合结构上恒为空——
> 也就是说，**哪怕 Policy 真被绕过、禁止工具真的执行了，评测也看不见。**
> 我刚才演示的这些拦截，当时评测根本没在量。
>
> 修掉之后，在干净提交上重跑，表面数字一个字都没变——
> 因为行为从来就是对的，变的只是检测器能不能看见。
>
> 这件事我写成了一篇单独的文章。
> 我认为它比这个项目本身更值得读：
> **一个 1.0 的指标，恰恰是最该被怀疑的那个。**

---

## 常见翻车点

| 现象 | 原因 | 处理 |
|---|---|---|
| 403 没出现，返回 401 | token 拼错或没带 `Bearer ` 前缀 | 检查 `Authorization` 头 |
| `$RUN_ID` 为空 | 上一步没接住返回值 | 先 `echo $RUN_ID` 确认 |
| 控制台空白 | 栈没起完 | 重跑 `wait-for-stack.sh` |
| 审计页没有 DENIED | 筛选条件没选对，或用了 viewer 之外的 token | viewer 可读审计 |
| 端口冲突 | 3000 / 8080 / 8081 被占 | `down -v` 后改 compose 端口 |

## 录完之后

```bash
docker compose -f deploy/compose/docker-compose.yml down -v
```

演示凭据 `dev-*-token` 由 `RUNBOOKGUARD_SEED_DEV_DATA=true` 注入，**仅限本地**。
任何真实部署都不该打开这个开关——录屏时如果镜头扫到 compose 文件，这一条值得顺口说一句。

---

## 实跑验证记录（2026-09-19）

本脚本的每条命令都在真实栈上跑过，不是从源码推断的。

前置：`docker compose -f deploy/compose/docker-compose.yml up -d --build`
→ `bash scripts/wait-for-stack.sh` → `bash scripts/smoke-m7.sh`
（**30 项全绿，0 失败**）。

| 演示步骤 | 期望 | 实测 |
|---|---|---|
| OPERATOR 建 Incident | 成功 | ✓ |
| VIEWER 建 Incident | 403 | **403** |
| AGENT 建 Run | 成功 | ✓ |
| AGENT 发起审批 | 成功 | ✓ |
| **AGENT 自批** | **403** | **403** |
| APPROVER 批准 | APPROVED | **APPROVED** |
| **审批后篡改参数再 consume** | **403** | **403** |
| 审计里有 DENIED 记录 | 有 | **12 条** |
| `grep "def approve"` 于 agent-runtime | 0 命中 | **0** |

**9 / 9 通过。**

环境：MacBook Pro (Apple M1 Pro) · macOS 15.2 · Docker 29.7.2 · fake provider，零真实模型调用。

> 首次构建若在 `mvn dependency:go-offline` 处失败并提示
> `Remote host terminated the handshake: SSL peer shut down incorrectly`，
> 那是 Maven Central 的网络抖动，重跑同一条命令即可——本次即是第二次才成功。
