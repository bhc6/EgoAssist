# 文档导航

## 当前说明

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
