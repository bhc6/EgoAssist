# EgoLife 独立题目测试协议：2026-09-14

本轮比较固定的 Omni 与模块化回答后端，使用此前项目未评测的官方 EgoLifeQA 题目。范围是官方描述/转写作为记忆来源的问答，不是原始视频音频理解，也不是完整官方排行榜成绩。

## 数据与独立性

数据源为公开的 [lmms-lab/EgoLife](https://huggingface.co/datasets/lmms-lab/EgoLife)，固定 revision `143fb319be7aa5ae210c936bf4f0f3a86092afb0`。官方 `EgoLifeQA/EgoLifeQA_A1_JAKE.json` 包含 500 道选择题，与此前本地使用的 `EgoLife_QA.json` 问答格式不同。

此前实际入库、评测的是 A1_JAKE DAY1 的 110 题和 22 条来源。本轮从 DAY2 至 DAY7 每天按 `SHA256('20260914:'+ID)` 顺序抽取 5 题，共 30 题。提问日和单一目标日必须都属于 DAY2 至 DAY7；无法解释为单一八位时刻的元数据排除。抽样只依赖 ID 和元数据，不看正确答案或模型表现。

排除 DAY1 提问 102 题、非单一保留日期目标 84 题、不兼容时刻 3 题，剩余 311 题中抽取 30 题。使用旧本地全部 33,352 条问答做规范化完全重复比较，而不只比较 110 条已评测题；30 题无完全重复。新来源与旧实际使用的 22 条来源也无规范化完全重复。

“独立”仅指相对于可追溯的项目开发/评测记录：未做语义近重复人工审计，无法排除云模型预训练污染。仍只有同一参与者，不能宣称跨参与者泛化。三个 seed 重复同一批题，不增加独立题数。测试结果一旦用于后续修改，这批题就成为已暴露回归集。

## 冻结与输入隔离

来源为 DAY2 至 DAY7 的 130 份官方 DenseCaption/Transcript SRT，解析为 110,387 个可追溯片段，其中描述 62,770、转写 47,617。使用标准 `srt` 解析器，不生成来源改写。

SRT 时间为文件名小时基准加相对区间。跨小时片段保留完整结束时刻；片段结束时刻不晚于提问时刻才允许入库。查询的八位 `HHMMSSxx` 时间保守向下取整到秒。使用 2000 年起点在内部表示相对 DAY 顺序，该日期不是实际拍摄日期，不提供给回答模型。

问题文件只含 ID、提问日/时刻、问题和四个选项。gold 答案、目标时间、类别及说明位于单独私有评估文件。检索只接收问题文本和提问时钟，不接收选项、答案、目标或关键词；回答模型接收问题、选项和共享检索证据，不接收 gold。所有预测持久保存后才读取 gold 评分。

固定 Timeline 默认 top-5、阈值 0.05、候选扫描上限 256 及时间、主体、指代、状态和来源约束。不补回被排除来源。每题断言 selected 属于 hard candidates，且所有候选来源已完整发生。大语料触及扫描上限时明确报告，不为改善分数临时修改限制。

模型为 Omni `qwen3.5-omni-flash-realtime`/Ethan 与模块化 `qwen-flash` + `qwen3-tts-flash-realtime`/Cherry，复用已实现的桥接流程。此轮输入已经是文字描述和转写，不调用两种后端的原始媒体识别能力。

## 顺序与判分

seed 17、43、91 仅控制问题执行顺序和两种后端先后交替，每个模式 90 次请求，共 180 次。不同题独立会话，不共享回答上下文。固定提示要求只返回 A/B/C/D，证据不足返回 ABSTAIN。不根据结果重试或修改提示。

主指标为与官方 gold 选项比对的准确率，无需 LLM Judge；拒答在该指标中记为未答对。另报完成率、格式错误、回答覆盖率、实际回答的证据支持度、输出延迟与调用量。支持度通过单独 qwen-flash 调用，只看问题、所选选项文本和检索证据，不看 gold；同模型家族的偏差不视作人工评审。

目标时间仅在预测后用于诊断，计算 top-1/top-5 描述片段是否覆盖目标的一秒时间窗、候选是否包含此类片段，以及截断到 top-5 的倒数排名。时间点覆盖不能证明语义相关性或完整证据覆盖，因此这些只是定位代理指标，不冒充标准 source Recall、全排名 MRR 或 nDCG。

按 gold 目标日/小时视频组聚合，同一题先平均三个 seed，然后对视频组做 2000 次配对 bootstrap。这个小时分组不是原始 30 秒视频编号，也不是跨参与者统计。

首段文字、首段音频从桥接层请求起算，排除检索、语义路由、麦克风端点和实体扬声器；生成结束不是听完。费用及没有测量的指标保留 null。模型调用缓存与音频播放缓存区分记录。

## 准备阶段修订

准备 r1 在目标时间格式解析停止；r2 在跨整点字幕解析停止；r3 完成数据冻结。此时还未运行回答模型，没有根据正确率调整数据。

首次入库在约 1.6 万条记录时中止，原因是查重 OR 查询逐次扫描账户历史。保持相同过滤和排序，改为两个索引查询的 UNION。107 项边界回归通过，另对已有数据做 21 组新旧查询比较，返回记录集合一致。r4 重新冻结代码，使用新数据库重跑；r3/r4 的题目和来源 SHA256 完全相同。入库性能修订不作为回答效果提升，之前未完成运行不并入成绩。

最终数据冻结：[freeze.json](../artifacts/egolife-independent-freeze-20260914-r4/freeze.json)。运行前协议、代码哈希、输入文件哈希、排除计数及来源文件哈希均保留。新旧查询验证见 [duplicate-query-equivalence.json](../artifacts/egolife-independent-modes-20260914/duplicate-query-equivalence.json)。

## 复现

```powershell
.venv\Scripts\python.exe -m pip install -e ".[egolife,modular]"
.venv\Scripts\python.exe scripts/egolife_independent_modes.py prepare --output artifacts/new-egolife-freeze
.venv\Scripts\python.exe scripts/egolife_independent_modes.py evaluate --dataset artifacts/new-egolife-freeze --output artifacts/new-egolife-evaluation
```

每次使用新目录。历史模型测试后的重新运行应标为回归，不再标作新的独立测试。`evaluate` 会验证冻结数据与生产/评测代码哈希；不匹配直接停止。
