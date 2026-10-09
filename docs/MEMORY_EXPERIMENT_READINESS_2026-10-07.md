# 记忆机制实验启动检查（2026-10-07）

后续实施说明：同日已在独立 **MemoryMechanismLab** 项目实现开发版四组运行器及 EgoAssist SourceIndex 适配；不再按旧建议在产品内新增 `mechanism_bias/`。本文保留实施前检查事实，最新边界见 [RESEARCH_BOUNDARY.md](RESEARCH_BOUNDARY.md)。正式模型、自然语言表示、独立数据和官方评分门槛仍未解除。

**结论：可以启动离线开发与链路预实验；当前不具备正式机制评测、独立 EgoLife 测试或论文涨点/降本结论的条件。** 本轮实际执行了抽样/数据核验、既有评测回归和隔离的旧链路冒烟测试，模型调用为 0。没有把“方案写完”视为“实验已实现”。

依据为[当前公开基准计划](PUBLIC_MEMORY_BENCHMARK_PLAN_2026-10-01.md)、[公开基准登记](PUBLIC_MEMORY_BENCHMARK_REGISTRY_2026-10-01.json)和[机制隔离协议](MEMORY_MECHANISM_CAUSAL_PROTOCOL_2026-10-01.md)。沿用当前 A/B/D 分阶段安排，不恢复旧版默认调用矩阵。

## 1. 实际通过的检查

| 项目 | 2026-10-07 检查结果 | 能说明什么 |
| --- | --- | --- |
| Mini-300 | 清单和脚本哈希一致；重新计算得到完全相同的 100+200 题；打乱库存输入顺序仍一致 | 抽样可复现；尚不等于测试已冻结 |
| LongMemEval 样本划分 | 100 题与剩余 400 题互斥，合计 500 | 元数据划分有效；完整 S 历史仍待核验 |
| MAB 原始文件 | 四个 Parquet 文件 SHA256 全部匹配；146 行、3,671 问；全部上下文和问题哈希/数量匹配 | 本地已下载文件及索引完整 |
| MAB 抽样索引 | 200 题逐项可映射回原文件；22 个配置均覆盖 | 没有行号/问题序号错配 |
| 跨库重叠 | MAB 小样本有 5 题与 LongMemEval 小样本直接重叠，与登记一致 | 两者不能宣称独立复现；历史家族和语义近重复审计仍未完成 |
| 现有评测回归 | `test_egolife_experiments.py`、`test_egolife_benchmark.py` 共 9 项；`test_controlled_retrieval.py`、`test_retrieval_policy.py` 共 29 项；38/38 通过 | 已有来源隔离、硬约束、缓存顺序、失败保留等局部契约通过；不是全项目验收，也不是新 G/V 实现验收 |
| 旧 Timeline 链路 | 隔离输出目录导入 22 个来源、完成 10 题检索；没有调用云端模型 | 旧链路可用于开发诊断 |
| 运行环境 | 项目 Python 3.13.7；Qwen 与 OpenRouter 密钥存在；隔离目录的 PyArrow 可读四个文件 | 只检查存在性，不证明密钥有效、余额足够或云端模型可达 |

完整性核验共 **28/28** 通过，证据为 [readiness-audit-r2.json](../outputs/experiment-readiness-20261007/readiness-audit-r2.json)。初次核验把问题哈希误当作原始文本哈希，出现 8 项不匹配；进一步核对发现库存使用小写及空白归一化，最终对全部 3,671 问匹配。初次记录 [readiness-audit.json](../outputs/experiment-readiness-20261007/readiness-audit.json) 保留，没有更改数据、问题选择或旧库存。

问题哈希复现规则为 `SHA256(' '.join(question.lower().split()).encode('utf-8'))`；上下文和文件仍按原内容计算 SHA256。检查只读取问题用于计算摘要，没有向模型提交问题、读取 MAB 答案列或输出测试题正文。

## 2. 正式运行仍缺少的条件

| 优先级 | 缺口及证据 | 对实验结论的影响 |
| --- | --- | --- |
| P0 | 协议指定的 `egoassist/eval/mechanism_bias/` 和 `scripts/run_memory_mechanism_study.py` 均不存在；G/V/R 登记仍为 planned | M00/M10/M01/M11 尚无可执行的机制对照；不能直接启动 300×4 |
| P0 | 同候选、同表示、真实 token 序列化、空开关不变性、跨适配器请求等价、长度耦合和结构负对照尚无新运行器验证 | 将旧系统对比直接当作机制消融，会混入工程差异 |
| P0 | LongMemEval 库存明确 `full_s_context_downloaded=false`；在检查的数据/缓存/研究目录中未找到完整 S 文件 | Oracle 元数据可用于选 ID，不能用其短历史代替正式 S 输入 |
| P0 | 项目暴露审计、近重复和历史家族分组未完成；EgoLife 新 100 题 selection_manifest 为空 | 未建立独立测试边界；MAB 冲突集的 800 问也不能当作 800 个独立历史 |
| P0 | 14 个执行控制字段为空：reader/provider/prompt/输出上限、tokenizer、parser/extractor/embedding/judge 版本及提示词、重试数、并发数 | 请求和评分无法复现；没有可审计的统一资源条件 |
| P0 | 阶段金额上限、价格快照、完整代码/依赖冻结、统计脚本与功效校准未完成 | 无法约束支出或保证结果可追溯；存在未提交修改时 HEAD 不能代表实际执行源码 |
| P1，外部比较前必须完成 | LightMem shared-input、StructMem transfer 的适配与原生配置校验尚未完成 | 不能将适配失误当作基线低分，也不能直接用子集分数减论文全量分数 |
| P1，第一视角主张前必须完成 | EgoLife 可见性、媒体时间对齐、过去前缀、帧/音频预算与新题暴露审计未完成 | 当前材料不足以证明真实眼镜感知—记忆—回答能力 |

正式状态在两份登记中均未冻结；本轮没有自动补默认模型或把空值改成 ready。缺失执行字段应由新的研究配置显式给出，不能悄悄继承生产默认。

本地 `data_real/egolife/A1_JAKE_DAY1/EgoLife_QA.json` 有 33,352 条原始行，但按旧 loader 的本地视频/来源/对话条件，仅有 **110 个可评问题、22 个去重来源、10 段视频**。原始行数不等于独立可用测试题数；旧数据仍属于已暴露开发材料。这一文件也不应与计划中的官方跨日 `EgoLifeQA_A1_JAKE.json` 测试轨混为一谈。

文件搜索有部分历史 `outputs/pytest-cache-*` 目录拒绝访问，因此本次不是整个磁盘的穷尽数据暴露审计；未据此宣称“其他位置绝无数据”或“泄漏审计完成”。

## 3. 旧运行器为什么不能直接代替新实验

- `scripts/run_egolife_memory_experiment.py` 可以执行 Timeline 导入和来源检索诊断，输出 sourceRecall/MRR 等；不生成并按官方标准评分完整回答。
- `scripts/run_controlled_retrieval.py` 使用旧 A/B/C/D 检索变体，要求至少三个 seed，并明确 `previously-exposed-exploratory`、独立测试题为 0；它不是 G/V 的 2×2 实现。
- 旧 controlled runner 的 judge 检查回答是否被引用证据支持，不读取 gold answer；**证据支持率不是官方问答正确率**。三个 seed 也不会增加独立样本数。
- `scripts/compare_memory_mechanisms.py` 使用字符预算，不符合新协议的统一真实 token 预算。

可以复用这些工具的契约、来源隔离和日志能力，但必须在新协议下重新实现并验证对照条件。

## 4. 本次离线冒烟结果及边界

命令：

```powershell
.\.venv\Scripts\python.exe scripts/run_egolife_memory_experiment.py `
  --dataset data_real/egolife/A1_JAKE_DAY1 `
  --max-questions 10 `
  --output outputs/experiment-readiness-20261007/legacy-timeline-smoke
```

该目录已经存在；重跑须使用新目录以保留证据。

结果：[results.json](../outputs/experiment-readiness-20261007/legacy-timeline-smoke/results.json)。10 题全部完成检索，5 题找回目标来源，5 题来源缺失；无非法引用或重复选择。检索中位耗时约 9.40 ms，**不包括 ASR、模型回答、评分、网络和语音输出**。sourceRecall@1 为 0.10，sourceRecall@5 为 0.50。

这是按既有顺序截取的 10 个已暴露问题，没有留出视频，且只运行一种旧配置。它只证明链路可执行；不能据此推断整体质量、涨点、模型回答延迟或新机制的价值。失败记录已保留。

## 5. 建议的启动顺序

1. **现在开始开发阶段。** 使用既有已暴露案例和人工最小反例，建立公共 source/candidate/selection/request/ledger 契约，接入强平面基线 M00。运行器必须隔离 gold、记录实际 tokens、保留失败、支持受控续跑和费用上限。
2. **实现最小 G/V 四组。** 先通过同池哈希、空开关、序列化、时间/人物/删除边界、长度与负对照检查；再做开发集小批真实调用，核验模型可达、评分器和全流程用量。这一步的输出仅用于调试/校准。
3. **并行准备独立数据。** 固定 revision 下载完整 LongMemEval-S 并核对 500 IDs；完成 MAB 历史家族分组和暴露审计；为 EgoLife 建立新的官方 QA、可见标注和媒体对齐清单。不能把当前 Mini-300 用作循环调参集。
4. **冻结后正式运行。** 固定配置、数据、代码/依赖、指标、统计和失败规则，再启动 A 期 Mini-300；外部适配未完成不影响内部开发，但不能省略后声称 A 期整体完成。第一视角 D 轨通过自己的数据与感知门槛后单独启动。

当前 A 期计划 1,400 次 reader 作答；加计划中的 D 轨为 1,800 次。它们**不是完整 API 调用次数或金额上限**，不含建库、抽取、embedding、judge、重试及额外机制控制。不能据此估计已经可承担的总费用。

检查没有发现阻止旧离线链路运行的错误；也没有验证整个工程基座完全无问题。当前最主要的启动障碍是新研究实验尚未落地和冻结，应优先补实验运行器及数据边界，而不是先运行大批测试再解释分数。
