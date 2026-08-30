# eval/ 目录说明

这里放的是评测的**证据**，不是代码。每个文件都是一次真实运行的产物，
删掉任何一个都会让对应的结论失去依据。

## reports/

| 文件 | 是什么 | 能不能引用 |
|---|---|---|
| `m6-frozen-round1-*.json` | 第 1 轮正式冻结评测。**未通过**（3 项未达标） | 可以，且**必须**保留——它是「第 2 轮不是为了数字改口径」这个论证的依据 |
| `m6-frozen-round2-*.json` | 第 2 轮正式冻结评测。10 项阈值全达标 | 可以。这是 M6 Gate 的依据 |
| `m6-real-model-20260829T143526Z.json` | 真实模型（glm-5.3-flash）12 个诊断 case | 可以，但**不参与阈值判定**——真实模型输出不可复现 |
| `m6-real-model-20260829T141112Z.json` | 同上，**加证据摘要之前**的一轮 | 只作为对比：成功率 0.1667 → 0.6667 是那次修复的效果证据 |
| `m6-real-model-20260829T144209Z.json` | 只跑 2 个 case 的诊断性重跑 | **不能当成评测结果**。它是为了查两个 `provider_failure` 的具体原因而跑的，样本量 2 |
| `dev-baseline-*.json` | M5 期间的非正式摸底 | **不能与 M6 的数字比较**：用的是 ADR-0009 之前的 `Diagnosis` schema |
| `retrieval-*.json` | Recall@K / MRR@K / citation validity | 可以 |
| `m3-real-provider/` | M3 的受控真实模型验证（4 次调用） | 可以 |

两轮冻结评测的 `working_tree_clean` 都是 `false`：评测跑在未提交的工作区上。
这个事实记在冻结清单里而不是被隐藏——否则「这个 commit 的代码」这句话不成立。

## traces/

每个目录对应一次评测运行，含 45 个 dev case 的 Trace。
Replay 用它们比对「同一配置的两次运行行为是否一致」。

目录名的 `round{N}` 与 reports 里的 `round{N}` 对应。
dry run（`--skip-held`）的目录名带 `dryrun-` 前缀，不会与正式评测混淆。

Trace 的摘要**不含**墙上时钟（时间戳在算 digest 前被规范化成 `<timestamp>`），
因此两次运行的 digest 可以直接比较。全部文本过 `redact()`。

## 纪律

- **正式评测只跑一次。** `scripts/eval-m6-frozen.py` 拒绝覆盖同一轮次的已有报告——
  覆盖等于销毁上一轮的证据。
- 第 2 轮及以后必须给 `--reason` 说明为什么重新冻结。
- 阈值未达标时**不能**修改判据口径或阈值定义（DEV_PROMPT §11）。
  只能回头改产品，重新冻结，用新的 `--round` 再跑一轮。
- `datasets/incidents-held/` 只在正式评测时使用。它的内容在开发期未被查看。
