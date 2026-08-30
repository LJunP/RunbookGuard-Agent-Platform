# M7 Gate 自检报告：产品交付

> 里程碑：M7（产品交付）
> 状态：**Gate 通过**（干净环境 `docker compose up --build` + 29 项冒烟全过）
> 日期：2026-08-29
> 上游依据：项目说明书 §3 / §12 M7，DEV_PROMPT §12 M7

---

## 0. 一句话结论

`docker compose up -d --build` 起 12 个容器，`scripts/wait-for-stack.sh` 六项就绪，
`scripts/smoke-m7.sh` **29 项全过**——含 CORS 白名单的正反两面、RBAC 三级、
完整审批链路（含自批被拒与篡改参数被拒）、三个服务的 Prometheus 端点被真实抓到、
以及 collector 收到新 span 的**计数增长**。

M7 补上了 M6 报告 §10 列出的 A2（Trace 端点）与 A3（单个审批 GET 端点）。
A1（提交后重跑冻结评测）、A4（checkpoint 落 MySQL）、A5（检索延迟拆分）仍未做，见 §7。

---

## 1. 交付物清单

### React 控制台（`apps/console-web/`）

| 文件 | 内容 |
|---|---|
| `src/api/client.ts` | HTTP 客户端 + `ApiError` 分类（401/403/409 各自不同处置） |
| `src/App.tsx` | 顶层壳、token 输入、错误提示翻译成「下一步做什么」 |
| `src/pages/IncidentsPage.tsx` | Incident → Run → Trace 三级下钻 |
| `src/components/TraceView.tsx` | 时间线 / 预算 / 证据与引用 / 审批链路，同屏四块 |
| `src/pages/ApprovalsPage.tsx` | 待审批与决策（控制台唯一的写操作） |
| `src/pages/AuditPage.tsx` | 审计事件，DENIED 高亮 |
| `src/components/format.ts` | 展示层纯函数（23 个单测覆盖） |
| `Dockerfile` / `nginx.conf` / `docker-entrypoint.sh` | 多阶段构建 + 运行时注入 API 地址 |

### Java 控制面新增

| 文件 | 内容 |
|---|---|
| `api/TraceController.java` | `GET/POST /api/v1/runs/{id}/trace` |
| `service/TraceService.java` | 步骤与证据的幂等写入 + 完整 Trace 读取 |
| `persistence/RunStepMapper.java` | 只增不改，唯一键 `(run_id, sequence)` |
| `persistence/EvidenceReferenceMapper.java` | `INSERT IGNORE`，evidence_id 是内容摘要 |
| `domain/RunStep.java` / `domain/EvidenceReference.java` | M0 §5 的两个实体 |
| `ApprovalController.get` + `ApprovalService.get` | `GET /api/v1/approvals/{id}`（补 M6 §10 A3） |
| `ControlPlaneConfig.addCorsMappings` | CORS 白名单，不允许携带 cookie |

### 观测（`observability/`）

| 文件 | 内容 |
|---|---|
| `otel/collector-config.yaml` | OTLP 收集 + 内存限制 + 属性脱敏 + 自省指标 |
| `prometheus/prometheus.yml` | 6 个抓取目标，间隔 15s |
| `grafana/provisioning/` | 数据源与面板 provider（`allowUiUpdates: false`） |
| `grafana/dashboards/runbookguard-overview.json` | 11 个面板，安全边界那一行放最上面 |
| `apps/agent-runtime-python/src/agent_runtime/observability.py` | 8 个指标 + OTLP 接入 + 路径归一化 |

### CI 与脚本

| 文件 | 内容 |
|---|---|
| `.github/workflows/ci.yml` | 5 条独立 job |
| `scripts/wait-for-stack.sh` | 六项 HTTP 就绪探测 |
| `scripts/smoke-m7.sh` | 29 项接线检查 |

---

## 2. 实际运行的测试与真实输出

### 2.1 Java（真实 MySQL / Redis / RabbitMQ，非 mock）

```
$ cd apps/control-plane-java && mvn -o test
[INFO] Tests run: 6, ... TenantIsolationIntegrationTest
[INFO] Tests run: 12, ... SecretRedactorTest
[INFO] Tests run: 9, ... IdempotencyIntegrationTest
[INFO] Tests run: 5, ... RateLimitIntegrationTest
[INFO] Tests run: 4, ... AuditImmutabilityIntegrationTest
[INFO] Tests run: 7, ... ConcurrencyAndTerminalStateIntegrationTest
[INFO] Tests run: 9, ... TraceApiIntegrationTest
[INFO] Tests run: 10, ... WorkerApiIntegrationTest
[INFO] Tests run: 15, ... HttpApiIntegrationTest
[INFO] Tests run: 13, ... LeaseIntegrationTest
[INFO] Tests run: 9, ... WorkerRecoveryIntegrationTest
[INFO] Tests run: 11, ... ApprovalSecurityIntegrationTest
[INFO] Tests run: 17, ... ArgumentsCanonicalizerTest
[INFO] Tests run: 25, ... DigestCrossLanguageVectorTest
[INFO] Tests run: 152, Failures: 0, Errors: 0, Skipped: 0
[INFO] BUILD SUCCESS
```

### 2.2 Python

```
$ cd apps/agent-runtime-python && .venv/bin/python -m pytest -q
540 passed in 4.24s

$ cd services/synthetic-lab && .venv/bin/python -m pytest -q
35 passed in 1.60s
```

### 2.3 控制台

```
$ cd apps/console-web && npm run typecheck
> tsc --noEmit
(无输出，通过)

$ npm test
 ✓ src/components/format.test.ts (23 tests) 41ms
 Test Files  1 passed (1)
      Tests  23 passed (23)

$ npm run build
✓ 33 modules transformed.
dist/index.html                   0.58 kB │ gzip:  0.51 kB
dist/assets/index-DV_jEoIk.css    3.49 kB │ gzip:  1.29 kB
dist/assets/index-CvdjEaS7.js   158.24 kB │ gzip: 52.17 kB
✓ built in 444ms
```

### 2.4 全栈就绪

```
$ bash scripts/wait-for-stack.sh
== 等待全栈就绪（每项最多 180s）==
control-plane    http://127.0.0.1:8080/actuator/health/readiness ok
agent-runtime    http://127.0.0.1:8100/health ok
synthetic-lab    http://127.0.0.1:8090/health ok
console          http://127.0.0.1:8081/index.html ok
prometheus       http://127.0.0.1:9090/-/ready ok
grafana          http://127.0.0.1:3000/api/health ok

ok    全部服务已就绪
```

### 2.5 M7 冒烟（29 项）

```
$ bash scripts/smoke-m7.sh
== 1. 服务可达 ==
ok    control-plane readiness
ok    agent-runtime health
ok    synthetic-lab health
ok    console index

== 2. 控制台的 API 地址被注入（不是构建期烧死的占位值） ==
ok    index.html 含 API base

== 3. CORS 放行控制台，拒绝陌生来源 ==
ok    预检放行控制台来源
ok    预检拒绝陌生来源

== 4. 认证与授权 ==
ok    无凭据 -> 401
ok    VIEWER 读 Incident -> 200
ok    VIEWER 建 Incident -> 403

== 5. 演示流程：Incident -> Run -> Trace -> 审批 ==
ok    创建 Incident
ok    创建 Run
ok    上报 Trace -> 200
ok    VIEWER 读回 2 步
ok    发起审批
ok    AGENT_RUNTIME 自批 -> 403
ok    APPROVER 批准 -> APPROVED
ok    篡改参数后 consume -> 403

== 6. 诊断链路（fake provider，零真实调用） ==
ok    结构化诊断返回 conclusion_type

== 7. 观测接线 ==
ok    agent-runtime /metrics
ok    control-plane /actuator/prometheus
ok    synthetic-lab /metrics
ok    Prometheus 抓到 control-plane
ok    Prometheus 抓到 agent-runtime
ok    Prometheus 抓到 synthetic-lab
ok    Grafana 数据源已 provision
ok    Grafana 面板已 provision

== 8. Trace 导出 ==
ok    agent-runtime trace 已启用
ok    collector 收到新 span

========================================
通过 29  失败 0
M7 冒烟通过。
```

---

## 3. Gate 条件自评

DEV_PROMPT §12 M7 的 Gate 条件：**在一台干净机器上照 README 操作，能完整复现演示流程。**

| 条件 | 自评 | 证据 |
|---|---|---|
| 一条命令起全栈 | **通过** | `docker compose up -d --build` 起 12 个容器 |
| 不需要额外配置即可运行 | **通过** | 默认 fake provider，无需任何模型凭据 |
| 演示流程可复现 | **通过** | 冒烟脚本把 README 的四步演示编码成断言 |
| 控制台可展示 Incident / Run / 时间线 / 证据引用 / 审批 / Trace | **通过** | 三个页面 + TraceView 四块 |
| CI | **通过（配置层面）** | 5 条 job；**未在 GitHub 上实跑**——仓库尚未推送，见 §7.1 |
| OpenTelemetry 接入 | **通过** | 两侧都导出，collector 端 span 计数增长可验 |
| Prometheus / Grafana 面板 | **通过** | 6 个抓取目标，11 个面板由文件 provision |

**「干净机器」这个条件的真实程度**：本次验证是在**已有构建缓存**的开发机上做的
（Maven 本地仓库、npm cache、Docker layer cache 都是热的）。
一台真正干净的机器需要额外下载依赖，首次 `--build` 会显著更久。
CI 的 compose job 是为了覆盖这一点，但它同样未实跑（§7.1）。

---

## 4. 关键设计决定与理由

### 4.1 控制台只有一个写操作

控制台能做的写操作只有审批决策。刻意不加「创建 Run」「重跑」之类的按钮：
业务真相的持有者是 Java 控制面，控制台加上写入口会模糊这条边界，
而且每个新写入口都是一个新的授权面。

审批界面**不提供编辑参数的入口**。审批绑定的是请求时算出的 `arguments_digest`，
让人在 UI 里改参数等于把「审批后篡改参数」这条攻击路径开在自己的界面上。
参数只读展示，供审批人核对。

### 4.2 token 在内存里，不进 localStorage

localStorage 里的 token 会被任何同源 XSS 拿走且长期有效。
控制台是演示与运维现场工具，刷新后重新粘贴可以接受。
这是一个明确的取舍，不是遗漏。

### 4.3 CORS 是白名单，且不允许携带 cookie

`allowedOrigins` 是显式清单（默认覆盖 compose 的 nginx 与本地 Vite）。
带凭据的跨源请求配 `*` 时浏览器会拒绝，于是很容易被改成「回显请求的 Origin」——
那等于对任何站点开放。冒烟脚本因此断言**两面**：放行控制台来源，
且对 `http://evil.example` 的预检不返回 200。

不开 `allowCredentials`：本项目的凭据走 `Authorization` 头，
开 cookie 只会引入 CSRF 面而没有任何收益。

### 4.4 控制台不代理 API

nginx 只发静态文件，浏览器直连控制面。走代理会让「哪些来源被允许」
这个决定从控制面转移到 nginx，而那台 nginx 的配置不在控制面的审计范围内。

### 4.5 API 地址运行时注入，不在构建期烧进 bundle

同一份镜像要能指向不同的控制面。烧进去会让「换环境」等于「重新构建」，
而 Gate 条件是「干净机器照 README 就能跑起来」。
冒烟脚本断言 `index.html` 里确实含被注入的地址。

### 4.6 指标标签里没有 tenant，也没有 run_id

租户与 Run 的数量都无上限，进标签就是基数爆炸——
`datasets/runbooks/rb-metric-cardinality-explosion--v1.0.0.md` 描述的正是这个故障。
`tests/test_observability.py` 有两个测试专门断言这一点，
另有 `normalise_path()` 把 URL 里的 id 段折叠成 `:id`。

租户维度的分析走审计事件，不走 Prometheus。

### 4.7 冒烟断言 span 计数**增长**，不只是端点可达

「配置了导出器」与「span 真的到了 collector」是两件事：前者只要写对 yaml，
后者要求网络可达、协议对得上、采样没把它全丢掉。只查配置会让一条断掉的
trace 链路看起来是好的。

两侧各打一次流量（Java 走 `micrometer-tracing-bridge-otel`，Python 走 `otel-sdk`），
因为只测一侧无法发现另一侧的链路断了。

### 4.8 Trace 只增不改

`RunStepMapper` 与 `EvidenceReferenceMapper` 都没有 update/delete 方法。
一个能被改写的 Trace 等于没有 Trace。

`run_step` 用唯一键 `(run_id, sequence)` + `INSERT IGNORE`；
`run_terminal_state` 反过来**不**用 IGNORE 而是让主键冲突抛异常。
两者语义不同：重复的终态是需要被审计的事件（可能是两个执行者在竞争），
重复的步骤只是消息重投。

`RecordResponse` 同时返回 `submitted` 与 `written`，两者不等就说明这批有重投。
只回一个 count 会让「消息投递三次」变得不可观测。

### 4.9 `GET /api/v1/approvals/{id}` 补上了一个真实缺口

在它存在之前，`ControlPlaneApprovalGateway.fetch()` 只能扫 pending 列表，
因此对**已决**的审批一律返回 `UNKNOWN`——Policy 无法区分「已批准」与「查不到」，
只能保守拒绝，`AWAITING_APPROVAL` 恢复后永远执行不了动作。

404 与 403 返回 `UNKNOWN` 而不是抛异常（审批不存在是业务事实），
但 500 抛异常（控制面坏了不是业务事实）。`_unknown_fact()` 的三个绑定字段全空，
使 Policy 的四项比对必然失败——「查不到」在授权判定上必须等价于「不放行」。

---

## 5. 实际踩到的问题

| # | 现象 | 根因 | 只靠什么才能发现 |
|---|---|---|---|
| 1 | 控制台容器起不来：`sed: can't create temp file` | `sed -i` 是「写临时文件再改名」，需要**目录**可写；`COPY` 进来的目录属 root，而 nginx-unprivileged 以 uid 101 运行 | 真的起容器。Dockerfile 本身没有语法错误 |
| 2 | collector 启动即退出：`failed to create "prometheus" exporter for data type "traces"` | Prometheus exporter 只处理 metrics 管道，挂到 traces 管道上 collector 拒绝启动。我要的是 collector **自省**指标，那是 `service.telemetry.metrics`，不是 exporter | 看容器日志。compose 的 healthcheck 没配到它，`up -d` 返回成功 |
| 3 | `synthetic-lab /metrics` 返回 404 | 代码加了端点但镜像没重建 | 容器化冒烟。本地 pytest 全绿 |
| 4 | 4 个新剧本对应的 case 全部 `repeated_state_detected`（M6 期间） | 同上：镜像里没有新剧本，工具调用全 404，证据集恒空导致指纹不变 | 同上 |

第 3、4 条是同一类：**代码改了但镜像没重建**。这是本项目第四、五次出现
「单测全绿而容器行为不同」。M7 的应对是把 `docker compose build` 写进 CI 的
compose job，让重建不依赖人记得。

第 1、2 条的共同点是：**`up -d` 返回成功不代表服务在工作**。
`wait-for-stack.sh` 用 HTTP 探测而不是只看 compose 退出码，正是为此。

---

## 6. 可复现命令

```bash
# 全栈
docker compose -f deploy/compose/docker-compose.yml up -d --build
bash scripts/wait-for-stack.sh
bash scripts/smoke-m7.sh

# 各语言测试
cd apps/control-plane-java && mvn test
cd apps/agent-runtime-python && python -m pytest -q
cd services/synthetic-lab && python -m pytest -q
cd apps/console-web && npm ci && npm run typecheck && npm test && npm run build

# 控制台开发模式（不走 nginx）
cd apps/console-web && npm run dev   # http://127.0.0.1:5173

# 清理
docker compose -f deploy/compose/docker-compose.yml down -v
```

---

## 7. 已知限制与未验证项

### 7.1 CI 未在 GitHub 上实跑（UNKNOWN）

`.github/workflows/ci.yml` 的 5 条 job 只在本地逐条验证了它们要跑的命令
（`mvn test`、`pytest`、`npm test`、compose build/up/smoke），
**workflow 本身未被 GitHub Actions 执行过**——仓库尚未推送。

因此以下几点是 `UNKNOWN`：
- runner 上的 Testcontainers 是否正常（本地是 Docker Desktop，runner 是 Docker CE）
- compose job 在 runner 的资源限制下是否会超时（12 个容器 + 首次构建）
- `setup-java` / `setup-python` / `setup-node` 的缓存键是否命中

推送后第一次运行大概率需要调整。这一点不该被写成「CI 已通过」。

### 7.2 「干净机器」是热缓存机器

§3 已说明。本次验证的机器有 Maven / npm / Docker 三层缓存。
真正的干净验证依赖 §7.1 的 CI compose job。

### 7.3 控制台没有组件渲染测试

23 个测试全部是展示层纯函数（颜色语气、除零、摘要前缀）。
组件渲染没测，因为它的价值在于「操作者能不能看懂」，那需要人看，
而不是快照测试全绿。这是一个取舍：一次 CSS 改动导致布局错乱不会被 CI 发现。

### 7.4 Grafana 匿名只读是本地专用配置

`GF_AUTH_ANONYMOUS_ENABLED=true` + `GF_AUTH_DISABLE_LOGIN_FORM=true`
免去了演示时的登录步骤。任何可被外部访问的部署都必须关掉它。
compose 文件里有注释，但**没有机制阻止**误用。

### 7.5 Java 侧的 Prometheus 端点未鉴权

`/actuator/prometheus` 与 `/actuator/health` 不需要 token。
指标里不含租户数据（§4.6），因此泄漏面是「有多少请求、多少 5xx」这类聚合信息。
在本地演示场景可以接受；生产部署应当把 actuator 端口与业务端口分开并限制来源。
**这一点必须在部署文档里写明**——M8 补。

### 7.6 其余未验证项（延续 M6）

| 项 | 状态 |
|---|---|
| 提交代码后重跑冻结评测（M6 §10 A1） | **未做**。两轮报告的 `working_tree_clean` 都是 false |
| checkpoint 元数据落 MySQL（M6 §10 A4） | **未做**。仍在内存 |
| LangGraph checkpointer | 仍是 `InMemorySaver`，非 `AsyncSqliteSaver` |
| 检索 latency / cost 按阶段拆分（M6 §10 A5） | **未做** |
| 动作工具进程隔离（M0 §9） | **未做**。仍在进程内。M8 |
| Trace 与 OTel span 的关联 | **未做**。`RunTrace` 与 span 是两套 id，控制台无法从 Trace 跳到 span |
| RabbitMQ Prometheus 插件 | 3.13 内置 `/metrics`，抓取配置已写但**未验证队列指标真的有值**——本次演示没有产生 MQ 流量 |

---

## 8. 下一里程碑的前置依赖（M8）

| # | 事项 | 现状 |
|---|---|---|
| B1 | **安装 kind 或 k3d**（`brew install kind` / `brew install k3d`） | 需要用户执行。kubectl v1.36.1 已在 |
| B2 | 核验 kind 默认 CNI（kindnet）是否真的执行 NetworkPolicy | 未核验。若不执行，说明书 §20 的 NetworkPolicy 要求无法验证，需换 Calico 或 k3d |
| B3 | 三类健康检查分离（liveness / readiness / startup）的 manifest | 未做 |
| B4 | 动作工具的进程/容器隔离 | 未做。这是 M0 §9 的硬要求，M8 必须落地 |
| B5 | actuator 端口分离与来源限制（§7.5） | 未做 |
| B6 | vLLM 与模型网关的职责边界文档 | 未做。**没有真实 GPU，只能写架构理解与客户端契约，不能写成性能验证** |

---

## 9. 简历状态

M6 Gate 已通过，因此项目**可以**写进简历。M7 让它变得可演示：
一条命令起全栈，一个脚本证明 29 项接线正确。

写的时候要连带说明 §7.1（CI 未实跑）与 M6 §7.1/§7.2（held 非独立、
冻结评测用脚本 provider）。这三条如果省掉，面试现场会被一个问题问穿。
