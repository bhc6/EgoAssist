# EgoLife 受控检索实验协议（修订）

此前所有已读、已回测、已用于改规则的题目均属探索性数据。`outputs/egolife_split_manifest.json` 的旧 test 标签不能代表独立测试集，重切分不恢复独立性。当前本地数据为 **1 位参与者、9 个有问答的视频、22 个来源、110 道问题**；DAY1 不是公历日期。不宣称跨参与者泛化，也不把来源注释检索称为视频理解。

## 历史结果的有效性

- 旧 `TimelineAdapter` 曾将 `self.records` 全部补入候选，没有重跑时间、身份、状态约束；其候选召回不能证明安全召回改善。
- Timeline 的 `candidates` 是截断到 20 项的审计视图，包含拒绝项。旧 Hybrid、EvidenceFirstPolicy 直接重排这个列表也不构成安全候选实验。
- 旧“RRF”混入原始相似度，不能作为纯排名融合的实验结果。
- `scripts/compare_memory_mechanisms.py` 曾将目标帧写入检索查询、索引和回答上下文。相关旧结果带目标泄漏；该入口已修正。
- 旧 runner 虽记录 seed，但对相同完整题集运行，没有用 split 隔离调参与验证。重复相同指标不是独立重复试验。

原 JSON/日志保留，通过相邻 `validity.json` 和历史文档开头标注失效范围、原文件哈希。数值不被删除或改造成新实验结论。

## 固定候选机制

| 实验 | 固定机制 | 消融 |
| --- | --- | --- |
| A | 当前工作区 Timeline 默认评分／阈值／top-5；去掉 adapter 的字符加分、意图规则和候选补回 | 基线；不声称恢复到不可追溯的旧代码版本 |
| B | 单词 BM25 与字符二元组 BM25，各自产生排名后等权 RRF | B_word、B_char |
| C | B 的词法排名与 Qwen text-embedding-v3 语义排名纯 RRF | C_semantic |
| D | 原文、句子、相邻双句多粒度 BM25，片段两路 RRF 后按最高片段分聚合来源 | D_sentence |
| E | 开发集选出 A/B/C/D 检索器，在校准视频选择拒答阈值 | 与同一检索器、不加选择策略的回答对比 |

所有排序使用稳定 source ID 打破并列；A 保留 Timeline 原有并列顺序。BM25 `k1=1.2,b=.75`，RRF `k=60`，top-k=5。没有问题关键词意图加分，不把相似度当概率。D 输出字符起止位置，可逐字切回原文，不生成改写来源。未得到支持的候选只保留为实验实现，不自动进入生产默认路线。

## 共享硬约束及数据隔离

`TimelineMemory.retrieve(include_hard_candidates=True)` 显式返回 `hardCandidates`：先执行 user/case/scope/active/kind 查询，再执行时间、指代、主体、关系、实体、场景检查。词法重叠与分数阈值属于检索机制，不能与硬约束混为一谈。原审计视图保持兼容。候选数据库扫描仍有统一上限，逐题报告是否截断；本地 22 条来源未达到上限。

所有 A—D 使用相同 Timeline 与来源库，并逐 seed 断言 hard candidate ID 完全一致。E 只能减少回答。来源入库严格只接收 `sourceId/videoId/text`；逐题检索只接收 `RetrievalQuery(question)`。不传 sample key、gold source ID、目标帧或 expected answer。gold 只在检索返回后加入结果行用于计算指标。视频分组仅用于评估，不用来缩小该题检索范围。

入库时间和查询时钟固定为 `2026-09-12T00:00:00Z`，只控制工程复现，**不代表视频发生日期**。没有真实日历锚点的数据不能支持真实时间准确率。时间、身份、纠正和来源边界由专门回归用例验证。

## 评估与保留条件

seed 固定为 `0,1,2`。按视频分 development / calibration / reused_validation；若同一来源跨组则直接报错。所有分组仍属于已经暴露的数据。每 seed 随机化机制运行顺序，缓存命中单独记录；不能把三个 seed 当作三位参与者。

先保存 `plan.json`，再运行检索与回答。开发视频宏平均 MRR 优先，Recall@5、Recall@1 用于并列选择 A—D。E 的候选阈值固定为 `0,.01,.025,.05,.1,.2,.4,.8,1.01`，分数差归一化后决定回答；校准集中至少 5 个被评审的回答、经验不支持率不高于 10% 才可选择非空策略，否则标记证据不足并澄清，不把零覆盖算成功。

报告来源 Recall@1/@5、全排名 MRR、nDCG@5、安全候选召回、按视频宏平均和分视频值。单一 gold 注释用于来源排名，不把其缺失直接视为答案错误。回答模型仅看问题和最多五个来源；另外一次 Qwen-flash 调用评审回答是否被所引用的原文支持。评审不看 gold 答案；ID 存在本身不能通过证据支持评审。同模型的独立调用仍有相关偏差，需要人工审计，不能视作人工答案准确率。

报告回答覆盖率、不支持回答比例、引用有效率、检索时延、入库时延、provider 请求时延、调用次数和缓存命中。费用与无法测量的指标为 `null`；API 失败不伪装成错误答案零分。公共回答／评审按问题和相同证据缓存，所有机制使用相同回答以隔离检索因素。

按视频 bootstrap 2000 次报告与 A 的配对差值和 95% 区间。保留为部署默认项需要：硬约束回归全部通过；联合排名指标和证据支持度无退步；MRR 配对区间有正向证据；延迟／调用开销可解释；全新独立测试与人工证据审计完成。当前数据无法满足独立测试条件，只能输出探索性候选，不能宣布优化成功。

## 运行

```powershell
.venv\Scripts\python.exe scripts/run_controlled_retrieval.py `
  --dataset data_real/egolife `
  --output artifacts/controlled-retrieval-<unique> `
  --seeds 0,1,2 --workers 4
```

输出目录必须不存在。`--offline` 将真实语义与回答评审标为不可用，不用模拟向量充数。边界回归：

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_controlled_retrieval.py tests/test_egolife_experiments.py tests/test_retrieval_policy.py tests/test_timeline_memory.py tests/test_memory_grounding.py
```
