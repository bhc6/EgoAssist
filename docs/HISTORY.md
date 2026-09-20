# 历史实现、实验与演示索引

这里保留各次实验、演示和失败诊断。当前推荐入口与实现选择见[文档导航](README.md)。日期最新不等于质量最佳，旧实验指标不能作为当前验收结果。

## 近期记录

- [EgoLife 原文证据复测](EGOLIFE_SOURCE_RETEST_2026-09-17.md)：原文证据与原 Timeline 同题同模型对照、180 次真实回答、原始请求与标准答案逐题比较；含大规模索引开销修复。
- [原始证据底座与统一回答上下文](SOURCE_EVIDENCE_ARCHITECTURE_2026-09-17.md)：独立历史导入、原文 FTS、完整审计、真实 LoCoMo 开发验证与时间失败修复；校正后可回答题最终证据召回 72.82%，未达 80% 目标。
- [证据删除、快照生命周期与评测口径修复](SOURCE_EVIDENCE_LIFECYCLE_FIX_2026-09-17.md)：删除传播、异步结果写入保护、按依赖保留快照，以及原始实验重算。
- [独立个人日记实现与真实验收](DIARY_IMPLEMENTATION_2026-09-17.md)：独立归档与页面、三链路分块压缩、670 项回归、真实日/周/月生成、超时恢复与录屏，明确内容质量边界。
- [个人日记与周期报告现状检查](DIARY_REPORT_AUDIT_2026-09-17.md)：三链路取材、月度覆盖、账户级归档、页面实测和实施验收标准。
- [EgoLife 历史视觉链路修复](EGOLIFE_HISTORY_FIX_2026-09-16.md)：657 项测试通过、真实历史图像复查与回答传递、修复后录屏；两道题的剩余错误明确保留。
- [EgoLifeQA 完整长录屏](EGOLIFE_LONG_VIDEO_2026-09-16.md)：单条超过十分钟的原速录屏，两题各输入完整本地素材，保留真实等待与标准答案对比。
- [EgoLifeQA 原题录屏与标准答案对照](EGOLIFE_BENCHMARK_VIDEO_2026-09-16.md)：两道官方原题、完整输入与回答、事后揭晓 ground truth，保留错误及证据不足。
- [EgoLife 连续视频与语音实录](EGOLIFE_MULTIMODAL_VIDEO_2026-09-16.md)：220 帧、8 轮真实语音问答、可播放录屏与逐轮音画证据。
- [生活化多轮对话视频](NATURAL_STORY_VIDEOS_2026-09-16.md)：四个完整长对话、38 轮真实输入、双向声音、问题修复与原始日志。
- [四场景真实链路展示](STORY_SHOWCASE_2026-09-15.md)：老人、职业记录、生活任务、无障碍交互的真实运行、修复、证据与现场讲稿。
- [项目 README](../README.md)：启动、三条核心链路、配置与运行边界。
- [三链路主架构](../ARCHITECTURE.md)：职责、执行边界与代码归属。
- [模块化实时后端](MODULAR_REALTIME_2026-09-14.md)：ASR、VL、文本与流式 TTS 的切换方式、Omni 对照和验收边界。
- [实时异常处理记录](LIVE_FAILURE_REMEDIATION_2026-09-14.md)：百炼账户拒绝、停止无效重试、状态响应压缩与恢复步骤。
- [两种实时模式回测](REALTIME_MODE_BACKTEST_2026-09-14.md)：三个固定 seed、同输入问答与媒体对照、完整应用语音控制、任务流程失败和成本口径。
- [EgoLife 留出题目测试](EGOLIFE_INDEPENDENT_RESULTS_2026-09-14.md)：30 个新题目、两种模式各 90 次真实调用、低准确率和共享检索限制；[冻结协议](EGOLIFE_INDEPENDENT_PROTOCOL_2026-09-14.md)。
- [EgoLife 检索损失诊断](EGOLIFE_RETRIEVAL_DIAGNOSIS_2026-09-14.md)：90 次检索对照、候选预算与字符 BM25 重排、排序饱和证据；已暴露回归分析，不是新独立测试。
- [EgoLife 向量检索实测](EGOLIFE_VECTOR_RESULTS_2026-09-14.md)：真实 Qwen embedding、全库召回与同池重排、360 条结果及边界校验；[实验协议](EGOLIFE_VECTOR_PROTOCOL_2026-09-14.md)。
- [EgoLifeQA A-D 四选一统一对照](EGOLIFE_ABCD_MCQ_2026-09-14.md)：最新完成的 360 次有效回答；准确率 A/B/C/D 为 27.78%/21.11%/32.22%/20.00%，含全指标表、评分口径及复现入口。C 的组级优势尚未确立，继续保留 A。
- [A/C 全量数据与论文对齐](EGOLIFE_AC_FULL500_ALIGNMENT_2026-09-14.md)：JAKE 500 题及七天历史已冻结；全量回答未启动，论文模型访问及最终对齐口径待确定。
- [记忆机制](../MEMORY_ARCHITECTURE.md)：实际数据流、时间语义与失效模式。
- [EgoLife 受控实验协议](EGOLIFE_EXPERIMENT_PROTOCOL_2026-09-12.md)：历史结果有效性修订、A—E 对照、消融与证据保留条件。
- [EgoLife A—E 实测结果](EGOLIFE_CONTROLLED_RESULTS_2026-09-12.md)：旧开放问答探索，未测量标准选项准确率；三个 seed、137 项回归、真实 Qwen 评审及不提升候选为默认方案的证据。
- [开发约束](../CONTRIBUTING.md)、[提示词](../egoassist/prompts/README.md)。
- [架构整改与验证](ARCHITECTURE_REMEDIATION_2026-09-11.md)：入口拆分、删除传播、增量投影和本地验收。

## 历史实现与验收

以下日期文档保留当次实验与失败证据，不自动代表当前默认参数、测试数量或性能。当前安装依赖只维护在 `pyproject.toml`；[旧环境快照](validation/requirements-windows-py313-historical.txt) 不是完整锁文件或安装指南。

- [三链路主架构](../ARCHITECTURE.md)：核心职责、双向影响、执行边界与代码归属。
- [跨链路闭环验收](CROSS_LANE_REVIEW_2026-09-10.md)：来源校验、主动澄清、记忆纠正、刷新去重和真实并发延迟。
- [观察、交互与记忆独立运行](OBSERVATION_LANES_2026-09-10.md)：独立视觉调度、历史背景与问题快照、真实并行测量及限制。
- [视频事件分割调研](STREAMING_VIDEO_EVENTS_RESEARCH_2026-09-10.md)：流式边界、动作与重要性提取、硬件成本、当前差距及对照实验。
- [硬编码改造验收](HARDCODING_REFACTOR_2026-09-10.md)：八项整改、后台记忆恢复、多轮工具、真实语音与尾延迟。

- [项目 README](../README.md)：启动、能力、配置、验证和部署。
- [记忆机制](../MEMORY_ARCHITECTURE.md)：实际数据流、时间语义、报告与失效模式。
- [提示词](../egoassist/prompts/README.md)：模板与真实模型请求审计。
- [代码审查](CODE_REVIEW_2026-09-09.md)：本轮检查范围、修复、剩余风险与验证。
- [自然打断实现与验收](INTERACTION_IMPLEMENTATION_2026-09-09.md)：本地停播、输出权限、原答续播、真实 Qwen 证据。
- [工具链路验收](TOOL_USE_2026-09-10.md)：默认自动执行、原问题绑定、真实工具 API、前端凭据和失败记录。
- [开放观测验收](OPEN_OBSERVATION_2026-09-10.md)：移除默认行为与地点白名单，静默记忆、原始采样证据及视觉报告。

## 架构决策

| 文档 | 主题 |
| --- | --- |
| [ADR 003](ADR_003_LIVEKIT_AND_MULTI_INTENT.md) | 媒体传输与多意图工作账本 |
| [ADR 004](ADR_004_ARTIFACTS_AND_HUMAN_HELP.md) | 资料与人工求助 |
| [ADR 005](ADR_005_CHECKPOINT_BOUND_PERCEPTION.md) | 绑定检查点的观察证据 |
| [ADR 006](ADR_006_PRIVACY_EVALUATION_GOVERNANCE.md) | 隐私与评价治理 |
| [ADR 007](ADR_007_AMBIENT_OBSERVATION.md) | 无任务观察授权与边界 |

## 历史研究与验收

日期文档描述当次代码、提示词和实验，不自动随代码更新。旧测试数量、默认参数和延迟不能视为当前值；失败证据继续保留。

- [记忆机制对比](MEMORY_MECHANISM_COMPARISON_2026-09-09.md)
- [人际交流与时间记忆](SOCIAL_TEMPORAL_MEMORY_2026-09-09.md)
- [Skill 结构](SKILLS_ARCHITECTURE_2026-09-09.md)、[生命周期](SKILL_LIFECYCLE_VALIDATION_2026-09-09.md)
- [记忆和 Skill 延迟](MEMORY_SKILLS_VALIDATION_2026-09-09.md)
- [早期真实验收](REAL_VALIDATION_2026-09-08.md)、[延迟分析](LATENCY_VALIDATION_2026-09-08.md)
- [归档证据](validation/)、[相关工作](RELATED_WORK.md)、[早期项目设想](PROJECT_CHARTER_2026.md)

`outputs/` 不提交 Git，其中产物不一定随克隆获得。重新运行脚本会生成新的证据目录，不应覆盖已有归档。
