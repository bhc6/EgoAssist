# TimelineMemory 构建层修复（2026-10-09）

本次修复针对 TimelineMemory 的**写入/建库**缺陷，而不仅是检索。默认行为对已有部署保持保守；新能力通过开关启用。

## 改动一览

| # | 缺陷 | 修复 | 位置 |
|---|---|---|---|
| 1 | 无主体+代词的卡片写入即判死 | `EGOASSIST_MEMORY_REFERENCE_POLICY=strict(默认)/demote/off`；demote 分数 ×0.25；任何策略下未解析指代都不拿 memory_key | `core/settings.py`, `memory/timeline.py` |
| 2 | 非偏好的 pending 卡永不激活 | `pending_claims()` / `resolve_pending()`；`GET/POST /api/memory/pending`（校验归属、拒绝=软删除、写审计） | `timeline.py`, `service.py`, `application/memory_api.py`, `composition.py` |
| 3 | 每行 SRT 一张碎卡、只有起点 | `memory/event_units.py`：按间隔≤8s、跨度≤30s 合并成事件卡，带 `[start,end]`、结构化说话人、佩戴者主体、全部源 id；实时观察卡由下一次观察闭合区间 | `event_units.py`, `service.py`, `timeline.close_open_interval` |
| 4 | 写入时用原文改写时间 | `metadata.timeBasis ∈ {measured, source-media, synthetic-relative-day}` 时不再按文本改时间；「多一点/有一点/一点儿」不再被当成 01:00；「刚才/刚刚/just now」不再关闭覆盖 | `grounding.py`, `temporal.py` |
| 4b | 离线视觉管线用入库时刻打时间戳 | `TimelineAssistant(video_origin=…)` 写真实视频区间；确定性 card_id 防重复；不再写 `[no visual content]`；`all_captions` 不再截断为 20 | `qa/timeline_pipeline.py`, `memory/adapters.py` |
| 5 | scene 只写不读；LongEvents 只在 journal；无真实分层 | scene 进入默认检索；LongEvents 每个修订写成 long 层 event 卡（新修订替换旧修订，源 id 保留，可级联删除）；`build_digests()` 把相邻 long 层卡聚合为摘要卡（抽取式、不删子卡） | `timeline.py`, `long_events.py`, `service.consolidate` |
| 6 | trigram FTS 匹配不了 2 字中文；跨标点伪 n-gram；疑问词稀释；key 不归一；无语义通道 | schema v4：`search_terms` 列 + unicode61 `memory_terms_fts`；n-gram 按 CJK 连续段切；查询去疑问停用词；`memory_key_for` 归一（我的钥匙/钥匙 × 位置/location/地点 → `location:钥匙`）；FTS 重建改用 meta 标记（原 count 比较永远不触发）；可选向量通道 `EGOASSIST_MEMORY_EMBEDDER=none(默认)/hashing/dashscope`，向量表对卡片 `ON DELETE CASCADE` | `timeline.py`, `memory/semantic.py` |

## 开关

| 环境变量 | 取值 | 默认 |
|---|---|---|
| `EGOASSIST_MEMORY_REFERENCE_POLICY` | strict / demote / off | strict |
| `EGOASSIST_MEMORY_EMBEDDER` | none / hashing / dashscope | none |

检索结果 `routing.referencePolicy`、`routing.semanticChannel` 会记录本次使用的设置。

## EgoLife 30 题检索消融（当前代码，非冻结协议）

数据：`artifacts/egolife-independent-freeze-20260914-r4`。命令：
`python scripts/egolife_independent_modes.py retrieve --dataset <freeze> --output <out> --unit event|cue [--resume --max-seconds N]`

| 配置 | 候选池命中 | Top1 | Top5 | MRR@5 | 每题选中证据总时长 |
|---|---|---|---|---|---|
| 旧结果（9-14 文档，旧代码） | 4/30 | 0/30 | 0/30 | – | – |
| cue + 新索引, strict | 4/30 | 0/30 | 1/30 | 0.017 | 10.6 s |
| cue + 新索引, demote | 4/30 | 0/30 | 1/30 | 0.017 | 10.6 s |
| event, strict / demote / off | 18/30 | 2/30 | 4/30 | 0.090 | 129.9 s |
| event + text-embedding-v3, strict | 19/30 | 2/30 | 4/30 | 0.090 | 129.9 s |

结果存于 `artifacts/timeline-build-fix-20261009/`。

**必须一起报告的限制：**

- 指标是「选中卡片与金标时间点是否重叠」的代理，不是答案正确率。事件卡每张覆盖约 26 s，cue 约 2 s，**命中率的提升有一部分来自覆盖时长变大**，不能单独解读为排序变好。
- 这 30 题此前已被多次使用（consumed holdout），不是独立测试集；本次未据此调参。
- 事件卡带佩戴者主体后不再触发指代规则，所以三种指代策略在事件卡上结果完全相同；在 cue 卡上这 30 题也无差别（9-14 诊断中仅 1 个目标被指代门拦截）。放宽规则的效果需在 110 题受控集上再测。
- 向量通道只多带来 1 个候选命中，Top5 不变：默认权重下语义分（≤0.6）通常低于 BM25 分，只能「救回零词面重叠的卡」。是否调权重应在独立集上决定。
- `evaluate` 冻结协议会校验 `timeline.py`/`grounding.py` 哈希，旧冻结数据集在新代码上会拒绝运行（设计如此）；新的正式评测需要重新 `prepare` 冻结。

## 回归测试

环境：Python 3.13.16、SQLite 3.53.1（与项目 venv 一致），对比 `git HEAD` 基线，105 个测试文件逐块跑。

- 基线与当前唯一共同失败：`tests/test_temporal_reminders.py` 3 项（需要下载 Temporal 测试服务器，环境问题）。
- 当前无新增失败。
- 有意修改的旧断言：`test_v1_database_migrates…`（版本 3→4）、`test_default_retrieval_excludes_legacy…`（scene 现可检索）、`test_realtime_episode_memory_persists…`（同上）；两个验证 strict 契约的测试显式固定为 strict。
- 新增测试：`test_reference_policy`、`test_pending_claims`、`test_pending_claims_api`、`test_event_units`、`test_observation_intervals`、`test_time_rewrites`、`test_consolidation_layers`、`test_index_channels`、`test_timeline_pipeline`，以及 `test_egolife_independent_modes` 中的事件单元不泄漏未来证据测试。
- 在 demote/off 与 hashing 向量通道下重跑记忆相关测试均通过。

本机复现：

```
pytest tests/test_reference_policy.py tests/test_pending_claims.py tests/test_pending_claims_api.py tests/test_event_units.py tests/test_observation_intervals.py tests/test_time_rewrites.py tests/test_consolidation_layers.py tests/test_index_channels.py tests/test_timeline_pipeline.py tests/test_timeline_memory.py tests/test_memory_grounding.py tests/test_realtime_episodes.py
```
