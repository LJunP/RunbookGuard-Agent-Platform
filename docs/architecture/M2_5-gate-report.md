# M2.5 Gate 自检报告

- 里程碑：**M2.5 — Synthetic Lab**（DEV_PROMPT 在说明书之上追加的里程碑，见 ADR-0001 C-1）
- 报告日期：2026-08-27
- 结论：**M2.5 Gate 两条件全部通过（有真实输出）。**

---

## 1. Gate 条件逐条自评

DEV_PROMPT §12 M2.5 Gate 原文：**「同一个故障剧本连续跑 3 次，产出的指标与日志特征稳定可复现；能通过 HTTP 查询到符合只读工具契约的数据。」**

| # | 条件 | 自评 | 证据 |
|---|---|---|---|
| G1 | 同一剧本连续跑 3 次特征稳定可复现 | **通过** | 5 个剧本各跑 3 次，时间戳归一化后 snapshot 的 SHA-256 完全相同（§3.2）。判定标准比 Gate 要求更严：不是"特征稳定"而是"逐字节相同" |
| G2 | HTTP 可查到符合只读工具契约的数据 | **通过** | 5 类接口全部 200，契约见 `contracts/http/synthetic-lab-v1.yaml`。容器化后同样通过（§3.4） |
| G3 | 至少实现 4 类故障 | **通过** | 实现 5 类：连接池耗尽、MQ backlog、5xx 升高、OOMKilled，外加 Prompt Injection 日志 |
| G4 | 故障剧本格式定义 | **通过** | ADR-0004 + 5 个 YAML 剧本 + 封闭的 shape 词表 |
| G5 | 5 类接口（ADR-0001 C-5 裁定新增运行时事件） | **通过** | metrics / logs / deployments / queues / runtime-events |

---

## 2. 交付物清单

### 2.1 决策与契约

| 文件 | 内容 |
|---|---|
| [docs/adr/ADR-0004-synthetic-lab-scenarios.md](../adr/ADR-0004-synthetic-lab-scenarios.md) | 剧本格式（声明式 YAML）、相对时间轴、seed 驱动确定性、5 类接口、剧本互斥、注入载荷不过滤 |
| [contracts/http/synthetic-lab-v1.yaml](../../contracts/http/synthetic-lab-v1.yaml) | OpenAPI 3.1，10 个端点，8 个错误码 |

### 2.2 源码（`services/synthetic-lab/`，8 个文件）

| 文件 | 职责 |
|---|---|
| `src/synthetic_lab/determinism.py` | blake2b 派生的确定性伪随机。不用 `random.Random`——它的输出取决于调用顺序 |
| `src/synthetic_lab/shapes.py` | 封闭的信号形态词表：flat / ramp / step / spike / sawtooth |
| `src/synthetic_lab/scenarios.py` | 剧本模型、YAML 解析、校验、fingerprint |
| `src/synthetic_lab/generator.py` | 按查询窗口即时计算指标 / 日志 / 部署 / 队列 / 运行时事件 |
| `src/synthetic_lab/app.py` | FastAPI 路由、剧本生命周期、互斥判定、alignment 判据 |
| `src/synthetic_lab/main.py` | uvicorn 入口 |
| `Dockerfile` | 非 root 运行 |
| `pyproject.toml` | 依赖锁定到确切版本 |

### 2.3 剧本（5 个）

| 剧本 | 对应 M0 场景 | 关键设定 |
|---|---|---|
| `db-pool-exhaustion-v1` | S1 | 唯一的完整正向基准；池使用率精确达到 1.0（`noise: 0`） |
| `mq-backlog-v1` | S2 | 生产速率平稳（排除性证据）+ 一次无关部署作诱饵 |
| `service-5xx-config-v1` | S3 | 错误率用 `step` 形态与部署时刻对齐；下游 synthetic-auth 指标正常 |
| `oomkilled-v1` | S4 | 内存 `sawtooth` 触顶 + 7 次 OOMKilled(137) + 累计 restart_count；**刻意不放应用级 panic** |
| `prompt-injection-logs-v1` | S6 | 四种注入形态原样返回，标记 `injected=true` |

### 2.4 测试与脚本

| 文件 | 内容 |
|---|---|
| `tests/test_synthetic_lab.py` | 35 项 pytest |
| `scripts/verify-m2_5-determinism.sh` | 28 项真实 HTTP 校验 |
| `deploy/compose/docker-compose.yml` | 新增 `synthetic-lab` 服务 |

---

## 3. 真实运行输出

### 3.1 单元 / 接口测试

```
$ cd services/synthetic-lab && PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
...................................                                      [100%]
35 passed in 0.98s
```

### 3.2 确定性校验（M2.5 Gate 硬条件）

```
$ bash scripts/verify-m2_5-determinism.sh

== 前置检查 ==
ok    synthetic-lab 健康：{"status":"ok","scenarios":5,"active":0}

== 剧本目录 ==
ok    已加载 5 个剧本
      db-pool-exhaustion-v1          seed=20260826   fingerprint=ccc3bcbda01f7dd7...
      mq-backlog-v1                  seed=20260827   fingerprint=63ad930bb56d9a0e...
      oomkilled-v1                   seed=20260829   fingerprint=7a84dd53e5076b25...
      prompt-injection-logs-v1       seed=20260830   fingerprint=d665bee4f6e4a8c3...
      service-5xx-config-v1          seed=20260828   fingerprint=ed26fc7240b9cd00...

== 确定性校验：每个剧本连续跑 3 次 ==
ok    db-pool-exhaustion-v1
      snapshot digest = 9e6ff4b2d66872c5652a1ed80e4bcf7c...
ok    mq-backlog-v1
      snapshot digest = ecaf4c8581073be2e4184dc24c4ef667...
ok    service-5xx-config-v1
      snapshot digest = be0b7d8d0343355ddbfe74f149339d5c...
ok    oomkilled-v1
      snapshot digest = 7998b9e54ee1058ff0505ab697380534...
ok    prompt-injection-logs-v1
      snapshot digest = d2363234be7fe6b622b094e37d940316...

== 基线模式（未启动剧本）==
ok    未启动剧本时返回基线数据（61 个点，非空）
ok    基线数据同样可复现

== 参数校验（非法输入必须拒绝，不静默用默认值）==
ok    未知剧本 -> 404                                [404]
ok    未知服务 -> 404                                [404]
ok    未知指标 -> 404                                [404]
ok    未知队列 -> 404                                [404]
ok    负窗口 -> 422                                  [422]
ok    窗口超上限 -> 422                              [422]

== 剧本互斥 ==
ok    启动 db-pool-exhaustion-v1 -> 200              [200]
ok    同一剧本再启动 -> 409                          [409]
ok    另一作用于同服务的剧本 -> 409                  [409]
ok    不同服务的剧本 -> 200                          [200]

== 覆盖范围外不插值 ==
ok    窗口超出覆盖范围返回空 + outside_scenario_window（不插值）

== 五类接口可查（只读工具的数据来源）==
ok    1. metrics                                     [200]
ok    2. logs                                        [200]
ok    3. deployments                                 [200]
ok    4. queues                                      [200]
ok    5. runtime-events（ADR-0001 C-5 新增）          [200]

== 部署记录不含配置值（威胁 T-3）==
ok    部署记录只有键名，无配置值

== 注入载荷原样返回（S6 依赖）==
ok    四种注入形态原样取回，未被过滤
ok    注入行被标记 injected=true（4 行），便于 grader 定位诱导来源

===============================
PASS=28  FAIL=0
EXIT=0
```

### 3.3 容器化后重跑（同一脚本，目标改为容器）

```
$ docker compose up -d --build synthetic-lab
$ docker ps --format '{{.Names}} {{.Status}}' | grep synthetic
rg-synthetic-lab Up 20 seconds (healthy)

$ bash scripts/verify-m2_5-determinism.sh
PASS=28  FAIL=0
EXIT=0
```

### 3.4 五类接口的实际返回样例

`db-pool-exhaustion-v1` 启动后：

- `GET /v1/metrics?service=synthetic-orders&metric=db_pool_active` → 71 个点，峰值精确等于 50（`db_pool_max` 亦为 50，使用率 1.0）
- `GET /v1/logs?service=synthetic-orders&query=connection pool` → 含 `connection pool exhausted: timeout acquiring connection after 5000ms (pool=orders-primary)`
- `GET /v1/deployments?service=synthetic-orders` → `v1.4.2 → v1.5.0`，`changed_config_keys: ["orders.repository.connectionLeaseMode"]`
- `GET /v1/queues?queue=synthetic-notify.work`（mq-backlog-v1）→ depth 从 ~120 升至 ~48000，`publish_rate` 平稳
- `GET /v1/runtime-events?service=synthetic-report`（oomkilled-v1）→ 7 次 `OOMKilled` / `exit_code: 137`，`restart_count` 累计至 7

---

## 4. 开发过程中真实踩到的问题

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | 连接池使用率只到 0.89，S1 必需证据不成立 | 池上限与饱和值被加了 3% 噪声，"恰好打满"变成随机 | `MetricSpec` 增加 `noise` 字段，硬边界指标设 `noise: 0` |
| 2 | 使用率仍只到 0.92 | 采样是左闭右开，`ramp` 形态永远到不了 `incident` 值（5 分钟窗口 / 30s 间隔时峰值只到 90%） | 采样改闭区间 |
| 3 | 第四条注入载荷（`at_minute: 4`）查不到 | 日志时间上界用了最后一个采样点本身，落在它之后半个采样间隔内的行被静默丢弃 | 上界延伸一个采样间隔 |
| 4 | **`service-5xx-config-v1` 连续 3 次产出不一致（2 个不同 digest）** | T0 带秒数 → 查询窗口两端切在分钟中间 → 落在切口外的日志行被丢弃 → 启动时刻的秒数渗进输出。日志条数在 314~329 之间波动 | T0 对齐到整分钟 |
| 5 | 容器启动即崩：`no scenarios found` | `pyproject.toml` 缺 `package-data`，剧本 YAML 没被打进 wheel。本地 `PYTHONPATH=src` 一切正常 | 加 `[tool.setuptools.package-data]` |
| 6 | 冒烟脚本 f-string 语法错误 | 单引号 heredoc 内的 f-string 嵌套引号被 shell 转义破坏 | 改用 `.format()` |

第 4 条是本里程碑最有价值的发现。它只在 5 个剧本中的 1 个上暴露——另外 4 个恰好因为日志模板的 `per_minute` 与窗口边界的组合而没被切到。**如果 Gate 判定标准是"关键判据稳定"而不是"逐字节相同"，这个 bug 会通过验收，然后在 M6 冻结评测时以"同一 case 两次跑出不同结果"的形式爆出来**——而那时按纪律不能改口径，只能回头改产品重新冻结一轮。

第 5 条同样值得记：本地测试全绿、容器启动失败，是打包配置与运行方式不一致的经典表现。这也是为什么 Gate 要求容器化后重跑同一脚本。

---

## 5. 已知限制与未验证项

### 5.1 已知限制（设计决策）

1. **这不是真实的性能压力环境。** 数据按查询窗口即时计算，不存在真的内存耗尽或 CPU 争抢。**不得把 M2.5 的 OOMKilled 事件说成真实的内存耗尽验证。** 真实资源压力在 M8 用 Kubernetes 演练。
2. **剧本表达能力受 `shape` 词表限制**（flat / ramp / step / spike / sawtooth）。新形态需改代码扩词表，不允许在 YAML 里写逻辑——否则确定性无法保证。
3. **拒绝剧本叠加**，无法构造"多重故障"场景。若 M6 需要，要回 ADR-0004 重新裁决。
4. **T0 必须对齐整分钟**，因此剧本的时间精度上限是分钟。这是确定性的代价。
5. **`is_decoy` 字段对 Agent 可见**。当前 API 直接返回它，M4 接入只读工具时必须在工具层剥离——否则 Agent 可以直接读出"哪次部署是诱饵"，S2 的误归因检验失效。**这是 M4 的必做项，已列入 §6.2。**
6. **`alignment` 判据是基于剧本声明计算的，不是从生成数据反推的。** 例如 `error_rate_jump_matches_deployment` 检查的是"shape 为 step 且部署在 at_minute=0"，而非真的去数据里找跃变点。这足够作为剧本自检，但不能当作"数据确实呈现该特征"的独立证明。
7. **无认证。** synthetic-lab 是本地测试替身，监听 127.0.0.1，不含任何真实数据。若未来暴露到网络需要加鉴权——当前**不适合**在非本地环境运行。

### 5.2 未验证项

| 项 | 状态 |
|---|---|
| 8 个 M0 场景能否被 Agent 实际诊断 | **未验证**，无 Agent 实现（M4） |
| 只读工具契约与本服务的字段对齐 | **未验证**，工具在 M4 实现。契约已写但两侧未交叉校验 |
| 剩余 9 类故障（Redis 热 key、readiness 失败、下游重试风暴等） | **未做**，M6 前须补齐至 30~50 case |
| 长时间运行的稳定性（内存增长、句柄泄漏） | **未验证**，未做长跑测试 |
| 跨机器复现（另一台机器上同一剧本是否产出同一 digest） | **未验证**。理论上 blake2b + 整数运算与平台无关，但浮点舍入可能有差异，**标记为需在 M7 验证** |
| M0 五个正式阈值 | **未验证** |
| S0 学习计划状态 | `UNKNOWN`（M0 Gate 报告 Q1，仍未回答） |

---

## 6. 下一里程碑（M3：模型 Runtime）的前置依赖

M3 交付：FastAPI 骨架、Provider Adapter、**fake provider**（CI 默认）、结构化输出与 SSE 流式、一次受控的真实模型合成数据验证。

### 6.1 阻塞项

| # | 前置依赖 | 状态 |
|---|---|---|
| B1 | M2.5 Gate 验收 | 待验收 |
| B2 | Provider 抽象边界决策（哪些能力属于 Adapter、哪些属于调用方） | 需在 M3 开工时定，写 ADR-0005 |
| B3 | 真实模型调用的预算与凭据来源 | 需你提供：是否有可用的 OpenAI-compatible endpoint 与独立预算。**若暂时没有，M3 的"一次受控真实模型验证"只能标 UNKNOWN，不能用 fake provider 冒充** |

### 6.2 M3/M4 必须承接的 M2.5 遗留项

1. **只读工具层必须剥离 `is_decoy`**（§5.1 第 5 条）。否则 S2 的误归因检验失效。
2. **交叉校验工具契约与 synthetic-lab 契约**：字段名、类型、时间格式两侧一致。
3. **决定是否新增第 6 个只读工具 `get_runtime_events`**（ADR-0001 C-5 遗留）。若新增，属于对说明书 §14 五工具清单的偏离，需新 ADR。

### 6.3 M3 应先写的失败测试

按铁律二，不是 happy path：

1. 模型返回不合法 JSON → typed failure，**不静默修补后当成功**。
2. 模型返回合法 JSON 但不符合 Pydantic schema → typed failure。
3. Provider 超时 → typed failure，不重试到无限。
4. Provider 返回 429 → 按退避重试，超上限后 typed failure。
5. Provider 返回空响应 / 截断响应 → typed failure。
6. SSE 流中途断开 → 已消费的部分不被当成完整结果。
7. 响应中包含疑似凭据 → 出口脱敏（威胁 T-3）。
8. fake provider 与真实 provider 的响应结构一致（契约测试），否则 CI 通过不代表真实可用。

---

## 7. 可复现命令

```bash
cd /Users/lijunpeng/Desktop/open_source_project/RunbookGuard-Agent-Platform/services/synthetic-lab

# 首次准备
python3 -m venv .venv
.venv/bin/pip install 'fastapi==0.115.6' 'uvicorn[standard]==0.34.0' \
  'pydantic==2.10.4' 'pyyaml==6.0.2' 'pytest==8.3.4' 'httpx==0.28.1'

# 测试
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q

# 本地起服务
PYTHONPATH=src .venv/bin/python -m uvicorn synthetic_lab.main:app --host 127.0.0.1 --port 8090

# 或用容器
cd ../../deploy/compose && docker compose up -d --build synthetic-lab

# 确定性校验（Gate 硬条件）
cd ../.. && bash scripts/verify-m2_5-determinism.sh

# 手工探查
curl -s -X POST http://127.0.0.1:8090/v1/scenarios/db-pool-exhaustion-v1/start | python3 -m json.tool
curl -s 'http://127.0.0.1:8090/v1/metrics?service=synthetic-orders&metric=db_pool_active' | python3 -m json.tool
curl -s 'http://127.0.0.1:8090/v1/runtime-events?service=synthetic-report' | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8090/v1/scenarios/stop

# 清理
cd deploy/compose && docker compose down -v
```

---

## 8. 简历状态

**仍不允许写入简历。** M6 Gate 未通过。M2.5 交付的是评测与演示的数据基础设施，不是产品能力证明。
