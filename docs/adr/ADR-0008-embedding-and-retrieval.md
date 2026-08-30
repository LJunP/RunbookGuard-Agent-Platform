# ADR-0008：Embedding 与检索栈选型

- Status: Accepted
- Date: 2026-08-29
- 里程碑：M5（M4 Gate 报告 §6.1 的阻塞项 B2）
- 关联：[M0 §10 RAG 设计](../architecture/M0-product-brief.md)、[ADR-0005 Provider 边界](ADR-0005-provider-boundary.md)

## Context

说明书 §16 要求 RAG pipeline 为：`ingest → normalize → section-aware chunk → hash/version
→ lexical index + embedding → Qdrant → retrieve → fusion → rerank → context select
→ cited answer`，且明确要求「必须先做关键词基线再做向量检索，两者要能对比」。

需要决定 embedding 从哪来。三个候选：现有模型网关的 embedding 端点、本地推理、或退化成
纯词法检索。

## 核验结果（2026-08-29 实测）

对已在用的网关做了直接探测：

```
$ curl -X POST 'https://opencode.ai/zen/go/v1/embeddings' \
    -H 'authorization: Bearer <redacted>' \
    -d '{"model":"embedding-3","input":"connection pool exhausted"}'

HTTP=404
<!DOCTYPE html> ... <title>Not Found | opencode</title> ...
```

返回的是**网站的 404 HTML 页面**，不是 API 的 JSON 错误体。这说明该路径在网关上根本
不存在路由——它只代理 `/chat/completions`。

**因此"用网关的 embedding 接口"不是被权衡掉的，是不存在这个选项。**

## Decision

### 1. embedding 用 fastembed（本地 ONNX 推理）

选 `fastembed` 而非 `sentence-transformers`：

| | fastembed | sentence-transformers |
|---|---|---|
| 推理后端 | ONNX Runtime | PyTorch |
| 依赖体积 | 约 60 MB | 约 800 MB（含 torch） |
| 与 Qdrant 的关系 | 同一团队维护 | 无关联 |
| CI 可离线 | 是（模型缓存后） | 是 |

依赖体积不是主要理由，**与 Qdrant 同源**才是：向量维度、距离度量、tokenizer 行为由同一
方维护，不会出现"embedding 侧和索引侧对不上"这类难查的问题。

模型：`BAAI/bge-small-en-v1.5`，384 维。选小模型的理由是 Runbook 是我们自建的合成语料，
用词受控且短（每段几十到两百词），大模型的语义优势在这个语料上体现不出来，而它会让
CI 的首次下载从几十 MB 变成几百 MB。

**M6 冻结评测时 embedding 模型名与版本必须进冻结清单**（说明书 §17 要求冻结
model/provider，embedding 模型同属此列）。

### 2. lexical baseline 用 BM25，且先于向量检索交付

`rank-bm25 0.2.2`，纯 Python，无外部服务。

顺序是硬要求（说明书 §10）：**先有基线数字，再上向量检索**。理由不是流程洁癖——没有
基线就无法回答"embedding 带来了什么"。如果 hybrid 的 Recall@5 是 0.82，而 BM25 单独
就是 0.80，那么向量检索这部分工程量的收益是 0.02，这个事实必须能被说出来。

因此 M5 的交付分两批：先 BM25 + 指标，再 Qdrant + 指标 + 对比。

### 3. 混合检索用 RRF（Reciprocal Rank Fusion），不用分数加权

```
score(doc) = Σ_over_retrievers 1 / (k + rank_in_that_retriever)
```

`k = 60`。

**为什么不用分数加权**：BM25 的分数是无界的（依赖 IDF 与文档长度），余弦相似度在
[-1, 1]。要把两者加权就必须先归一化，而归一化方式（min-max、z-score、softmax）会引入
一个需要调的参数，且它对查询分布敏感。RRF 只用排名，不受分数量纲影响，也没有需要
调的权重——这与项目「可评测」的取向一致：少一个参数就少一个"数字是调出来的"的质疑。

代价：RRF 丢弃了分数的强弱信息。一个 BM25 分数远高于其余的文档，和一个略高的文档，
在 RRF 里贡献相同。这在我们的语料规模（30~50 文档）下不重要。

### 4. rerank 用词法覆盖度 + 段落类型先验，不引入 cross-encoder 模型

rerank 的实现是一个**可解释的确定性函数**：

- 查询词在段落中的覆盖率
- 服务名精确匹配加权（症状描述里的服务名是强信号）
- 段落类型先验：`symptom` 与 `diagnosis` 段对"这是什么故障"类查询更相关，
  `safe_action` 与 `rollback` 段对"怎么处置"类查询更相关

**为什么不用 cross-encoder**：它会再引入一个模型（体积、延迟、冻结项），而收益在 30~50
文档的语料上无法被可靠测出——Recall@5 的差异会小于数据集本身的噪声。更重要的是
可解释性：这个 rerank 的每一分都能说出来自哪条规则，而 cross-encoder 的分数不能。

**这是一处对说明书的偏离**：§16 的 pipeline 写了 rerank，但没规定必须用模型。本 ADR
把 rerank 实现为规则函数并如实标注，不声称"实现了神经 rerank"。

### 5. chunk 策略：section-aware，一段一 chunk，不做滑动窗口

Runbook 的六段结构（service / symptom / precondition / diagnosis / safe_action /
rollback）天然就是语义边界。一段一 chunk 的好处是：

- 引用可以精确到 `section_id`，而不是"第 3 个 chunk"
- 段落类型可作为 rerank 的先验
- 不需要 overlap，因此不会出现同一句话在两个 chunk 里、引用指向哪个都对的歧义

代价：长段落（超过 embedding 模型的上下文）会被截断。Runbook 是自建的，因此约束为
每段不超过 1500 字符，在 ingest 时校验，**超长直接拒绝入库而不是静默截断**——静默截断
会让引用的 content_hash 对应不到完整原文。

### 6. 元数据缺失一律拒绝入库

说明书 §16 列出 8 项必需元数据：`document_id`、`document_version`、`section_id`、
`service`、`content_hash`、`chunker_version`、`embedding_model`、`indexed_at`。

「缺元数据就无法做引用校验，这是硬要求」——因此实现为构造期强制，缺任一项抛异常。
不给默认值，尤其不给 `document_version` 默认 "1"：那会让两个不同版本的文档看起来同版本。

### 7. tenant 过滤在查询条件里，不在结果后过滤

Qdrant 的 filter 与 BM25 的候选集都在检索**之前**按 tenant 收窄。

理由（威胁 T-4）：结果后过滤意味着向量检索已经跨租户召回过了。一旦某处忘记过滤，
或者过滤逻辑有 off-by-one，跨租户数据就泄漏了。前置过滤让"忘记过滤"表现为"查不到
任何东西"，而不是"查到了别人的东西"。

## Consequences

**正面**：

- 无外部 embedding 依赖，CI 离线可跑、零成本、可复现。
- 基线先行使"向量检索的增量"成为一个可报告的数字。
- RRF 与规则 rerank 都没有需调参数，减少"数字是调出来的"质疑面。
- 元数据强制 + section 级 chunk 使引用可精确反查。

**负面 / 代价**：

- rerank 是规则而非模型，**不得声称实现了神经 rerank**。
- 首次运行需下载 embedding 模型（约 60 MB）。CI 需缓存，否则每次构建都下载。
- 小模型（384 维）在语义相似但用词不同的查询上弱于大模型。这是刻意的取舍，
  M6 的检索指标会如实反映它。
- RRF 丢弃分数强弱信息。
- 每段 1500 字符上限是人为约束，对更长的真实 Runbook 不适用。

## 验证方式

1. BM25 单独的 Recall@K / MRR@K 有数字。
2. hybrid（BM25 + 向量 + RRF + rerank）的 Recall@K / MRR@K 有数字，**且与基线对比**。
3. 缺任一元数据的 chunk 拒绝入库。
4. 超长段落拒绝入库，不静默截断。
5. 引用可反查 `document_version` + `section_id` + `content_hash`。
6. Runbook 更新后旧引用仍指向旧版本。
7. 跨租户 Runbook 检索不到。
8. 检索未命中时 `retrieval_hit=false` 可观测（S7 的正确弃答依赖它）。
