# M0 Gate 自检报告

- 里程碑：**M0 — 问题与边界冻结**
- 报告日期：2026-08-26
- 报告人：主力开发工程师（AI）
- 结论：**M0 交付物已完成，请验收。** 未进入 M1。
- 产品代码变更：**0 行**（M0 按定义不写产品代码）

---

## 1. 交付物清单与文件路径

| # | 交付物（DEV_PROMPT §12 M0 要求） | 文件路径 | 状态 |
|---|---|---|---|
| 1 | Product Brief | [docs/architecture/M0-product-brief.md](M0-product-brief.md) | 完成 |
| 2 | 用户与场景 | [docs/architecture/M0-users-and-scenarios.md](M0-users-and-scenarios.md) | 完成 |
| 3 | 不做清单 | [docs/architecture/M0-non-goals.md](M0-non-goals.md) | 完成 |
| 4 | 威胁模型 | [docs/architecture/M0-threat-model.md](M0-threat-model.md) | 完成 |
| 5 | 8 个初始故障场景 | [docs/architecture/M0-initial-incident-scenarios.md](M0-initial-incident-scenarios.md) | 完成 |
| 6 | 上游冲突裁决 ADR（DEV_PROMPT §2 / §16 要求） | [docs/adr/ADR-0001-scope-freeze-and-spec-conflicts.md](../adr/ADR-0001-scope-freeze-and-spec-conflicts.md) | 完成 |
| 7 | 交付物结构校验脚本（铁律二的载体） | [scripts/check-m0-docs.sh](../../scripts/check-m0-docs.sh) | 完成 |
| 8 | 本报告 | docs/architecture/M0-gate-report.md | 完成 |

内容规模（实测 `wc -l`，见 2.6）：文档 7 个共 1323 行，校验脚本 111 行。

**没有创建的目录**：`apps/`、`services/`、`contracts/`、`datasets/`、`deploy/`、`eval/`、`observability/`。按 DEV_PROMPT §2「按里程碑逐步长出来，不要一次性建空目录」，M0 只创建了 `docs/architecture/`、`docs/adr/`、`scripts/`。

---

## 2. 实际运行的命令与真实输出

### 2.1 铁律二：先写会失败的测试

M0 没有产品代码，因此"失败测试"的形式是**交付物结构校验脚本**：它把 DEV_PROMPT 对 M0 的每一项硬性要求（8 个场景、每场景 7 项定义、6 条必需威胁、17 条不做项各带理由等）编码成可执行断言。先写脚本、确认 36 项全部 FAIL、再写文档使其转绿。

**第一次运行（文档尚未创建）**：

```
$ bash scripts/check-m0-docs.sh
== M0-1 Product Brief ==
FAIL  Brief: 一句话定义  (缺少文件 docs/architecture/M0-product-brief.md)
FAIL  Brief: 问题陈述  (缺少文件 docs/architecture/M0-product-brief.md)
FAIL  Brief: 差异化（对标 HolmesGPT/kagent）  (缺少文件 docs/architecture/M0-product-brief.md)
FAIL  Brief: 成功标准  (缺少文件 docs/architecture/M0-product-brief.md)
FAIL  Brief: 三分钟答辩（M0 Gate 条件）  (缺少文件 docs/architecture/M0-product-brief.md)
FAIL  Brief: 事实与推测分离标注  (缺少文件 docs/architecture/M0-product-brief.md)
...（中间 26 条同为 FAIL，完整输出见下方说明）...
== M0-7 Gate 自检报告 ==
FAIL  Gate: 交付物清单  (缺少文件 docs/architecture/M0-gate-report.md)
FAIL  Gate: 真实运行输出  (缺少文件 docs/architecture/M0-gate-report.md)
FAIL  Gate: 已知限制与未验证项  (缺少文件 docs/architecture/M0-gate-report.md)
FAIL  Gate: 下一里程碑前置依赖  (缺少文件 docs/architecture/M0-gate-report.md)

===============================
PASS=0  FAIL=36
EXIT=1
```

首次运行 36 项全部失败，退出码 1。这是刻意的：脚本先于文档存在。

### 2.2 最终运行（本报告写入后）

```
$ bash scripts/check-m0-docs.sh 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | tail -8
== M0-7 Gate 自检报告 ==
ok    Gate: 交付物清单
ok    Gate: 真实运行输出
ok    Gate: 已知限制与未验证项
ok    Gate: 下一里程碑前置依赖

===============================
PASS=36  FAIL=0

$ bash scripts/check-m0-docs.sh >/dev/null 2>&1; echo "EXIT_CODE=$?"
EXIT_CODE=0
```

36 项结构断言全部通过，退出码 0。第 2.3 节保留的是**本报告写入之前**的第二次运行结果（PASS=32 FAIL=4，缺的 4 项正是本报告自身），用来显示转绿过程，不是修饰后的结果。

### 2.3 校验脚本逐行结果（本报告写入前的第二次运行，32 项通过）

```
== M0-1 Product Brief ==
ok    Brief: 一句话定义
ok    Brief: 问题陈述
ok    Brief: 差异化（对标 HolmesGPT/kagent）
ok    Brief: 成功标准
ok    Brief: 三分钟答辩（M0 Gate 条件）
ok    Brief: 事实与推测分离标注

== M0-2 用户与场景 ==
ok    用户画像 >= 4 个（P1..Pn）  (5 >= 4)
ok    用户旅程 >= 3 条（UJn）  (5 >= 3)
ok    用户: 明确非目标用户

== M0-3 不做清单 ==
ok    不做项 >= 17 条（说明书 9 条产品级 + 提示词 6 条过程级 + 技术栈排除）  (17 >= 17)
ok    每条不做项都有独立理由段  (17 >= 17)
ok    不做清单: 有解禁/重新评估条件

== M0-4 威胁模型 ==
ok    威胁条目 >= 6 条  (6 >= 6)
ok    威胁必含: 注入日志诱导越权
ok    威胁必含: 审批后篡改参数
ok    威胁必含: Secret 泄漏进 Trace
ok    威胁必含: 工具越 tenant 访问
ok    威胁必含: 无界循环烧预算
ok    威胁必含: 崩溃后重复副作用
ok    每个威胁绑定里程碑落点  (7 >= 6)
ok    威胁模型: 含信任分级表

== M0-5 初始故障场景 ==
ok    初始故障场景 = 8 个  (8 >= 8)
ok    每场景: 输入快照  (9 >= 8)
ok    每场景: 允许工具集合  (9 >= 8)
ok    每场景: 必需证据  (13 >= 8)
ok    每场景: 可接受结论  (9 >= 8)
ok    每场景: 禁止动作  (9 >= 8)
ok    每场景: 期望终态  (10 >= 8)
ok    每场景: grader  (11 >= 8)

== M0-6 ADR: 上游冲突裁决 ==
ok    ADR: 有 Status 字段
ok    ADR: 记录 >= 3 处说明书/提示词冲突  (7 >= 3)
ok    ADR: 声明冲突裁决原则

== M0-7 Gate 自检报告 ==
FAIL  Gate: 交付物清单  (缺少文件 docs/architecture/M0-gate-report.md)
FAIL  Gate: 真实运行输出  (缺少文件 docs/architecture/M0-gate-report.md)
FAIL  Gate: 已知限制与未验证项  (缺少文件 docs/architecture/M0-gate-report.md)
FAIL  Gate: 下一里程碑前置依赖  (缺少文件 docs/architecture/M0-gate-report.md)

===============================
PASS=32  FAIL=4
```

**校验脚本的诚实边界**：它只检查**结构**（文件存在、章节存在、条目计数达标），**不检查内容质量**。它无法判断威胁模型是否真的覆盖了攻击面、grader 语义是否可实现。M0 的内容质量只能由人工验收（这正是 Gate 存在的原因）。不要把 `PASS=36` 理解为"M0 内容正确"。

### 2.4 竞品事实核验（WebFetch，2026-08-26）

| 目标 | 核验方式 | 结果 |
|---|---|---|
| `github.com/robusta-dev/holmesgpt` | WebFetch | 仓库描述原文 `SRE Agent - CNCF Sandbox Project`；README 原文 "We are a Cloud Native Computing Foundation sandbox project." |
| `github.com/kagent-dev/kagent` | WebFetch | 仓库描述原文 `Cloud Native Agentic AI`；README 仅称 "kagent is a Cloud Native Computing Foundation project"，**无 sandbox/incubating/graduated 字样** |

由此发现两处上游文档不准确，已记入 ADR-0001 C-2：说明书 §31 的 HolmesGPT 链接组织名有误（应为 `robusta-dev`）；DEV_PROMPT §1 把 kagent 归为"SRE Agent"不准确（实为 K8s 原生通用 Agent 框架，Agent/ModelConfig/ToolServer 为 CRD）。

### 2.5 环境事实核验

```
$ python3 --version
Python 3.12.13
$ java -version 2>&1 | head -1
openjdk version "17.0.18" 2026-01-20 LTS
$ docker --version
Docker version 29.7.2, build a7dcaa6
$ bash --version | head -1
GNU bash, version 3.2.57(1)-release (arm64-apple-darwin24)
```

Python 3.12 与 JDK 17 与固定技术栈一致，Docker 可用。**注意**：系统 bash 为 3.2（macOS 自带），校验脚本已避免使用 bash 4+ 语法（无关联数组、无 `mapfile`）。

**未运行的验证**：MySQL / Redis / RabbitMQ / Qdrant 的可用性 —— M0 不需要，`未验证`。

### 2.6 交付物规模（实测）

```
$ wc -l docs/architecture/*.md docs/adr/*.md scripts/check-m0-docs.sh
     278 docs/architecture/M0-gate-report.md
     286 docs/architecture/M0-initial-incident-scenarios.md
     180 docs/architecture/M0-non-goals.md
     102 docs/architecture/M0-product-brief.md
     214 docs/architecture/M0-threat-model.md
     132 docs/architecture/M0-users-and-scenarios.md
     131 docs/adr/ADR-0001-scope-freeze-and-spec-conflicts.md
     111 scripts/check-m0-docs.sh
    1434 total
```

（M0-gate-report.md 的行数在本次编辑后略有变化，上表为编辑前的快照。）

---

## 3. Gate 条件逐条自评

DEV_PROMPT §12 M0 Gate 原文：**「能用 3 分钟讲清"为什么这不是普通聊天机器人"，且不做清单里的每一条都有理由。」**

| # | Gate 子条件 | 自评 | 依据 |
|---|---|---|---|
| G1 | 3 分钟讲清"为什么不是普通聊天机器人" | **需人工验收** | 定稿口径见 Product Brief §5，按 30 秒分四段：能力边界差别 / 三段分离（模型无权限）/ 不可信输入是数据 / 崩溃可恢复且可评测。文本已就绪，但"能否在 3 分钟内讲清"只能由验收人听一遍判定，我无法自证。标记为 **UNKNOWN（待人工验收）**，不标 PASS。 |
| G2 | 不做清单里每一条都有理由 | **通过** | 17 条 NG 每条含 `理由` 与 `重新评估条件` 两段，校验脚本断言"17 个 NG 条目 / 17 个理由段"均达标（见 2.3）。理由均说明"为什么它会破坏安全/可评测/可恢复"，不使用"不需要"这类无信息量表述。 |
| G3 | 不写任何产品代码，产出全在 docs/ | **通过** | `git status` 新增仅 `docs/`（6 个 md）与 `scripts/check-m0-docs.sh`。脚本是交付物校验工具，不是产品代码，不进任何运行时。 |
| G4 | 威胁模型覆盖 6 项指定威胁 | **通过** | T-1 注入日志诱导越权 / T-2 审批后篡改参数 / T-3 Secret 泄漏进 Trace / T-4 工具越 tenant 访问 / T-5 无界循环烧预算 / T-6 崩溃后重复副作用，每条含攻击路径、后果、缓解措施、里程碑落点、验证方式、残余风险。另补 T-7~T-12 二级威胁与 A1~A6 攻击者模型、INV-1~INV-6 不变式。校验脚本逐条断言通过。 |
| G5 | 8 个初始故障场景 | **通过** | S1~S8，每个含说明书 §17 要求的 7 项（输入快照 / 允许工具集合 / 必需证据 / 可接受结论 / 禁止动作 / 期望终态 / grader）。其中 5 个是失败路径、弃答或安全红线场景，只有 3 个是正向路径。 |
| G6 | 指出上游文档冲突（DEV_PROMPT §2 / §16） | **通过** | ADR-0001 记录 7 处（C-1~C-7），含 2 处**说明书内部**不一致（§8 四项动作工具 vs §14 三项）与 1 处技术张力（进程内运行 vs 强制 cancel）。每处给出裁决与影响。 |

**总体**：G2~G6 通过；**G1 待人工验收**。按纪律，Gate 的最终判定权在验收人，我不自称 M0 通过。

---

## 4. 已知限制与未验证项

### 4.1 未验证（无法在 M0 阶段验证）

| 项 | 状态 | 何时可验证 |
|---|---|---|
| 五个正式阈值（安全红线拒绝率 / Schema 合法率 / citation validity / 任务成功率 / 恢复唯一终态率） | **未验证**，无实现 | M6 |
| T-1~T-6 的所有缓解措施 | **未验证**，无实现 | M1~M6 分散落地 |
| INV-1~INV-6 六条不变式 | **未验证** | M1（INV-2/5）、M4（INV-1/3/4）、全程（INV-6） |
| 8 个场景能否在 synthetic-lab 稳定复现 | **未验证** | M2.5 |
| 各 grader 能否程序化实现（尤其 S8"是否显式提及矛盾"这类语义判定） | **UNKNOWN** | M6 前须明确；若只能用 LLM-as-judge，必须声明其可靠性，不得假装确定性判定 |
| 场景难度是否合理（会否全过或全不过） | **未验证** | M5 结束时用 `incidents-dev` 非正式摸底 |
| UJ 旅程的时间收益（"省多少分钟"） | **未验证**，且无真实用户前不做此类主张 | 不承诺 |
| 干净机器一键复现 | **未验证** | M7 |

### 4.2 已知限制（设计决策带来的，非缺陷）

1. **只读工具的 cancel 是协作式的**（ADR-0001 C-6）。进程内 async 执行无法硬取消；超时后 Run 侧放弃等待并标 typed failure，被放弃的协程可能继续占用资源到自然结束。缓解：工具自身 HTTP timeout 小于 Run 侧 timeout + 结果大小上限。此残余风险在 M4 工具契约中显式声明。
2. **注入可污染结论文本**（T-1 残余风险）。即使动作被 Policy 拒绝，注入内容仍可能影响诊断叙述。缓解是引用校验 + 审批人复核，无法完全消除。
3. **租户无物理隔离**。第一版是单实例多租户，靠应用层 binding + 查询条件过滤（T-4）。
4. **不存在真正的 exactly-once**（T-6）。做的是"至少一次投递 + 幂等执行 = 效果唯一"。推论：**任何自身不幂等的动作工具不允许进入动作工具集**，这条要写进 M4 工具契约。
5. **repeated-state 检测可能漏判**语义等价但表述不同的状态（T-5 残余风险）。
6. **A5（宿主机 root 攻击者）明确不在防护范围**，已在威胁模型中声明，不假装能防。
7. **校验脚本只校结构不校质量**（见 2.3）。

### 4.3 需要验收人回答的问题

| # | 问题 | 为什么现在问 |
|---|---|---|
| Q1 | **S0 学习计划是否已通过？** | 说明书 §32 要求"通过 S0 后再依据 M0 创建仓库和第一个里程碑"，而 DEV_PROMPT 要求现在就做 M0。M0 是纯文档，不冲突；但 **M1 起要写真实 Java + MySQL + Redis 代码**，若 S0 未过，M1 的起点需要重新商定。见 ADR-0001 C-4，当前状态 `UNKNOWN`。 |
| Q2 | 是否接受 ADR-0001 的 7 项裁决？ | 其中 C-3（动作工具定为 3 个，不加"暂停消费者"）和 C-5（M2.5 增加第五类容器事件接口）会改变后续里程碑的工程量。 |
| Q3 | 是否接受 M2.5 接口从 4 类变 5 类？ | 这是 S4（OOMKilled）成立的必要条件。不加则 S4 无法实现，M6 的 13 类覆盖不成立。 |

---

## 5. 下一里程碑（M1）的前置依赖

M1 交付：Incident / Run / Approval / Audit API；MySQL schema 与 migration；Redis 限流与缓存；RBAC；单元与集成测试。

### 5.1 阻塞项（必须先解决）

| # | 前置依赖 | 当前状态 | 处理 |
|---|---|---|---|
| B1 | M0 Gate 人工验收通过（尤其 G1） | 待验收 | 铁律一：未验收不得进入 M1 |
| B2 | Q1（S0 状态）明确 | `UNKNOWN` | 需验收人回答 |
| B3 | `arguments_digest` 参数规范化算法选型 | `UNKNOWN` | M1 第一件事：决策并写 ADR-0002。必须定义键排序、数值/Unicode 归一化、摘要算法；否则 T-2 的缓解无法实现，且会产生"合法请求被误拒"或"不同参数同摘要"两类缺陷 |
| B4 | MySQL 8 / Redis 可运行（Docker Compose 或 Testcontainers） | `未验证` | Docker 29.7.2 已确认可用；M1 集成测试必须连真实中间件，不用 mock（DEV_PROMPT §13） |

### 5.2 M1 应先写的失败测试（按铁律二，M1 开工第一步）

不是 happy path，而是这些：

1. **审批后篡改参数** → Executor 侧 digest 比对必须拒绝（T-2 核心，M1 就要落地校验逻辑）。
2. **Approval 过期后使用** → `expires_at` 已过必须拒绝。
3. **跨 tenant 访问 Incident / Run** → RBAC 必须拒绝并产生审计事件（T-4）。
4. **Incident 并发更新** → 乐观锁 `version` 冲突必须失败而非静默覆盖（T-6 的数据库层地基）。
5. **同一终态重复写入** → 数据库层唯一性约束兜底，不只靠应用层判断（INV-5）。
6. **审计事件被尝试修改/删除** → 必须失败（只追加不可篡改）。
7. **Secret 出现在异常响应或日志** → 脱敏断言（T-3）。
8. **限流阈值突破** → Redis 限流必须拒绝（T-10）。

### 5.3 M1 不做的事（避免范围蔓延）

不实现 Agent 逻辑、不接 RabbitMQ 消费（M2）、不接模型（M3）、不做前端（M7）、不建 synthetic-lab（M2.5）。M1 只把"业务事实的权威持有者"做正确。

### 5.4 跨里程碑待核验清单（铁律五）

| 项 | 里程碑 | 状态 |
|---|---|---|
| LangGraph 当日 API（node/edge/state/checkpointer/interrupt） | M4 | `UNKNOWN`，动手前核验并写 ADR |
| MCP 当日正式协议版本、SDK 支持、迁移说明 | M4 | `UNKNOWN`，动手前核验并写 ADR |
| OpenTelemetry GenAI 语义约定稳定性 | M7 | `UNKNOWN` |
| kind/k3d 默认 CNI 是否执行 NetworkPolicy | M8 | `UNKNOWN`，若默认不执行则需换 CNI，否则说明书 §20 的 NetworkPolicy 要求无法验证 |
| 动作工具沙箱实现方式（独立进程 vs 容器） | M4 | `UNKNOWN` |
| 是否新增第 6 个只读工具 `get_runtime_events` | M4 | `UNKNOWN`，会偏离上游 5 工具清单，需新 ADR |

---

## 6. 可复现命令

```bash
cd /Users/lijunpeng/Desktop/open_source_project/RunbookGuard-Agent-Platform

# 校验 M0 交付物结构（退出码 0 = 结构完整）
bash scripts/check-m0-docs.sh

# 去掉颜色码便于粘贴
bash scripts/check-m0-docs.sh 2>&1 | sed 's/\x1b\[[0-9;]*m//g'

# 只看失败项
bash scripts/check-m0-docs.sh 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep '^FAIL'

# 查看本次新增内容
git status --short
wc -l docs/architecture/*.md docs/adr/*.md scripts/check-m0-docs.sh
```

---

## 7. 简历与对外材料状态

**当前不允许把本项目写入简历。** 依据 DEV_PROMPT §12 M6 Gate：只有 M6 Gate 通过后才允许，且数字必须是真实测试结果。当前所有能力指标均为 `未验证`。

本仓库 README 已按此约束标注项目处于 M0，不含任何能力数字。
