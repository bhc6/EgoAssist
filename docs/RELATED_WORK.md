# EgoAssist 相关工作调研

> 整理时间：2026-08-01 | 供论文撰写参考

---

## 目录

1. [同类系统（Realtime Egocentric Assistant）](#1-同类系统realtime-egocentric-assistant)
2. [Egocentric Video Benchmarks](#2-egocentric-video-benchmarks)
3. [Memory-Augmented Agent（记忆机制）](#3-memory-augmented-agent记忆机制)
4. [Realtime 多模态对话基座](#4-realtime-多模态对话基座)
5. [Skill 路由 / Tool 调用](#5-skill-路由--tool-调用)
6. [EgoAssist 定位与差异化](#6-egoassist-定位与差异化)

---

## 1 同类系统（Realtime Egocentric Assistant）

### 最直接同类工作

| 论文 | 机构 / 年份 | 核心贡献 | 链接 |
|---|---|---|---|
| **Vinci / EgoVideo-VL** | CMU / Meta，2025.03 | EgoVideo-VL 将 egocentric 视觉基础模型与 LLM 融合，支持 scene understanding、temporal grounding、future planning，部署在便携设备 always-on 模式 | [arxiv 2503.04250](https://arxiv.org/abs/2503.04250) |
| **EgoLife** | NUS，2025.03 | AI 眼镜驱动的生活陪伴助手，长期记忆 + 个人效率提升；提供 EgoLifeQA benchmark（多日连续视频多轮 QA） | [arxiv 2503.03803](https://arxiv.org/abs/2503.03803) |
| **WearVQA** | —，2025.11 | 专针对可穿戴设备的 VQA benchmark，处理遮挡/低光/模糊等真实穿戴挑战 | [arxiv 2511.22154](https://arxiv.org/abs/2511.22154) |
| **WearVox** | Meta，2026.01 | 3,842 条多通道 egocentric 音频，5 任务：搜索 QA、闭卷 QA、旁说拒绝、**Tool Calling**、语音翻译 | [arxiv 2601.02391](https://arxiv.org/html/2601.02391v1) |
| **Web-Native Smart-Glasses Agents** | —，2026.03 | 在 EgoLife + HD-EPIC 上测试，与商业基线对比 task completion 和用户满意度 | [arxiv 2603.01104](https://arxiv.org/abs/2603.01104) |
| **Wearable Egocentric Contextual AI 架构** | —，2025.12 | 系统架构层面建模 always-on 空间感知可穿戴设备，偏工程 | [arxiv 2512.16045](https://arxiv.org/abs/2512.16045) |
| **Benchmarking Egocentric Multimodal Goal Inference** | —，2025.10 | 评估 wearable agents 的目标推断；人类 93% vs 最优 VLM 84%（多选） | [arxiv 2510.22443](https://arxiv.org/abs/2510.22443) |

---

## 2 Egocentric Video Benchmarks

### 核心数据集

| 数据集 | 规模 | 来源 / 年份 | 关键任务 |
|---|---|---|---|
| **Ego4D** | 3,670 h，931 人，74 地 | Meta / CMU 等，CVPR 2022 | NLQ, MQ, AV, SCOD, FHO |
| **Ego-Exo4D** | 同步第一 / 第三视角 | Meta，CVPR 2024 | 技能学习、姿态估计、Expert Commentary |
| **EPIC-Kitchens-100** | 100 h，厨房场景 | Bristol，2022 | 动作识别、目标检测 |
| **EgoLifeQA** | 随 EgoLife 发布 | NUS，2025 | 多日跨 session QA |

### Ego4D Episodic Memory Track（最相关子任务）

- **NLQ（Natural Language Query）**：给定 egocentric 视频 + 文字问题，定位答案时间段
  - 主要指标：R@1, IoU=0.3 / 0.5
  - 2024 年挑战赛冠军 [ObjectNLQ](https://arxiv.org/abs/2406.15778)：R@1=23.15
- **MQ（Moment Query）**：localize 特定动作发生的时间窗口
- **Grounded QA in Long Egocentric Videos**（CVPR 2024）：[arxiv 2312.06505](https://arxiv.org/abs/2312.06505)

### 最新 Egocentric Memory Benchmarks

| Benchmark | 时间 | 内容 | 链接 |
|---|---|---|---|
| **S-EMBER** | 2026.07 | **3,141 个视频，388 小时**，Ray-Ban Meta 眼镜录制；streaming egocentric memory retrieval，**与 EgoAssist 检索路径最直接对标** | [arxiv 2607.02689](https://arxiv.org/html/2607.02689v1) |
| **EgoMemReason** | 2026.05 | 三类记忆：entity memory（状态变化）、event memory（跨天回忆）、behavior memory（行为模式抽象） | [arxiv 2605.09874](https://arxiv.org/abs/2605.09874) |
| **EgoToM** | 2025.03 | 扩展 Theory-of-Mind 到 egocentric 视频 QA | [arxiv](https://arxiv.org/html/2503.22152v1) |

---

## 3 Memory-Augmented Agent（记忆机制）

### 基础框架（引用量高）

| 论文 | 机构 / 年份 | 核心贡献 | 开源 |
|---|---|---|---|
| **Generative Agents** (UIST 2023) | Stanford + Google | 定义 memory stream 架构：recency + importance + relevance 三维检索；reflection 高阶记忆合成；planning 层；25 agent 沙盒涌现行为 —— **后续大量工作的基础架构** | [GitHub](https://github.com/joonspk-research/generative_agents) |
| **MemGPT** (arXiv 2310.08560) | UC Berkeley，2023 | OS 隐喻：in-context 主存 + external 磁盘，LLM 自主发 function call 换页；进化为 [Letta](https://github.com/letta-ai/letta) | ✅ |
| **MemoryBank** (AAAI 2024 Most Influential) | —，2023 | Ebbinghaus 遗忘曲线动态衰减 + 再访强化，模拟人类记忆；个性合成模块 | [arxiv 2305.10250](https://arxiv.org/abs/2305.10250) |
| **ReadAgent** (ICML 2024) | Google DeepMind | 两遍阅读：压缩为 gist memory → 按需查完整片段；有效上下文延伸 3.5–20×，无需 finetune | [项目页](https://read-agent.github.io) |
| **A-MEM** (NeurIPS 2025) | AGI Research，2025.02 | Zettelkasten 启发：写入时 LLM 主动生成关键词 + 链接，动态互联知识网络 | [GitHub](https://github.com/WujiangXu/AgenticMemory) |

### 近期进展（2025–2026）

| 论文 | 时间 | 核心 | 链接 |
|---|---|---|---|
| **AgeMem** | 2026.01 | 统一长短期记忆管理，RL 优化；5 个长期 benchmark SOTA | [arxiv 2601.01885](https://arxiv.org/html/2601.01885v2) |
| **MMAG** | 2025.12 | 五层记忆：conversational / long-term user / episodic / sensory / short-term working | [arxiv 2512.01710](https://arxiv.org/abs/2512.01710) |
| **MemOS** | 2025.05 | 记忆中心执行框架，支持跨平台协调，可控 / 可适应 / 可演化 | [arxiv 2505.22101](https://arxiv.org/html/2505.22101v1) |
| **Agentic Memory** | 2025.02 | 图数据库 + 复杂记忆组织，超越平面向量存储 | [arxiv 2502.12110](https://arxiv.org/abs/2502.12110) |
| **MemInsight** | 2025.03 | Amazon；自主记忆增强，LoCoMo benchmark +34% recall | [arxiv 2503.21760](https://arxiv.org/html/2503.21760v1) |

### Memory 评测 Benchmarks

| Benchmark | 时间 | 内容 | 链接 |
|---|---|---|---|
| **MemBench** | 2025.06 | factual + reflective 记忆；effectiveness / efficiency / capacity 三维评估 | [arxiv 2506.21605](https://arxiv.org/abs/2506.21605) |
| **MemoryAgentBench** | 2025.07 | 多轮增量交互，4 种记忆能力，模拟真实增量信息处理 | [arxiv 2507.05257](https://arxiv.org/abs/2507.05257) |
| **PersonalWAB** | WWW 2025 Oral | 个性化 web agent，PUMA 框架（用户记忆对齐） | [GitHub](https://github.com/HongruCai/PersonalWAB) |

---

## 4 Realtime 多模态对话基座

| 论文 | 机构 / 年份 | 核心贡献 | 链接 |
|---|---|---|---|
| **Moshi** | Kyutai，2024.10 | speech-text foundation model；全双工，并行用户 / 系统语音流，Mimi 神经音频编解码；**端到端 speech-to-speech** | [arxiv 2410.00037](https://arxiv.org/abs/2410.00037) · [GitHub](https://github.com/kyutai-labs/moshi) |
| **Mini-Omni** | —，2024.08 | 首个完全开源端到端实时语音交互模型；text-instructed speech generation；VoiceAssistant-400K 数据集 | [arxiv 2408.16725](https://arxiv.org/abs/2408.16725) · [GitHub](https://github.com/gpt-omni/mini-omni) |
| **LiveKit Agents** | LiveKit，2025.04 GA | 生产级 WebRTC 语音 / 视觉 agent 框架；cascaded STT-LLM-TTS pipeline 或 speech-to-speech；MCP tool 支持；**EgoAssist 底层传输层** | [docs.livekit.io](https://docs.livekit.io/agents/overview/) |

> **EgoAssist 与 Moshi/Mini-Omni 的区别**：EgoAssist 采用 cascaded pipeline（STT → LLM → TTS），保留完整文本推理链，支持 tool calling 和 memory 注入；Moshi 是端到端 speech-to-speech，延迟更低但推理链不透明。

---

## 5 Skill 路由 / Tool 调用

| 论文 | 来源 / 年份 | 核心 | 规模 | 链接 |
|---|---|---|---|---|
| **AgentBench** | Tsinghua，ICLR 2024 | 首个多维 LLM-as-Agent benchmark，8 个交互环境，29 个 LLM | 8 环境 | [arxiv 2308.03688](https://arxiv.org/abs/2308.03688) |
| **TaskBench** | Microsoft Research，NeurIPS 2024 | 任务自动化三阶段：decomposition → tool invocation → parameter prediction；Tool Graph 表示 | 千级 tool 场景 | [arxiv 2311.18760](https://arxiv.org/abs/2311.18760) |
| **ToolLLM / ToolBench** | Tsinghua / Yale，ICLR 2024 Spotlight | 16,464 个真实 RESTful API；DFSDT 多步 API 推理；ToolLLaMA 匹配 ChatGPT tool use | 16,464 API | [arxiv 2307.16789](https://arxiv.org/abs/2307.16789) |
| **API-Bank** | Alibaba DAMO，EMNLP 2023 | 首个 tool-augmented LLM 专用 benchmark；73 工具，314 对话，753 次 API call | 73 工具 | [arxiv 2304.08244](https://arxiv.org/abs/2304.08244) |
| **SkillRouter** | —，2026.03 | 1.2B 参数 body-aware retrieve-and-rerank；top-1 accuracy 74%；13× 更小、5.8× 更快；**直接对标 EgoAssist `registry.route()`** | ~80,000 skills | [arxiv 2603.22455](https://arxiv.org/html/2603.22455v1) |
| **Compositional Skill Routing** | —，2026.06 | 复杂请求分解为原子子任务 → 每子任务检索 skill → 组合执行计划；对应 EgoAssist `parallel_candidates` fan-out | — | [arxiv 2606.18051](https://arxiv.org/html/2606.18051v1) |
| **AgencyBench** | —，2026.01 | 1M token 真实上下文，32 场景，6 类 agentic 能力，138 任务 | 138 任务 | [arxiv 2601.11044](https://arxiv.org/abs/2601.11044) |

---

## 6 EgoAssist 定位与差异化

### 独特卖点组合

```
EgoAssist
├─ Realtime egocentric wearable assistant（同 Vinci / EgoLife）
├─ 自研三级记忆机制
│   ├─ Pipeline 1: HierarchicalMemory（进程内存，视觉层次，帧级定位）
│   ├─ Pipeline 2: TimelineMemory（SQLite WAL，10 种 kind，BM25 检索）
│   └─ RealtimeEpisodeMemory（统一 realtime 入口，包装 Pipeline 2）
├─ 自动意图路由 → Skill → Tool → Task lifecycle
├─ task-preference 学习（propose → confirm → retrieve 闭环）
└─ 跨 session 记忆持久化（SQLite WAL，支持多用户隔离）
```

### 与同类工作的核心差异

| 维度 | EgoAssist | 同类工作的不足 |
|---|---|---|
| **记忆 kind 体系** | 10 种 egocentric 专用 kind（scene / observation / tool-effect / task-preference…） | MemGPT / AgeMem 等均为通用对话记忆，无场景专用分类 |
| **task-preference lifecycle** | propose → confirm → retrieve 完整闭环，跨 session 提升 | 无同类工作覆盖任务级偏好学习 |
| **多模态写入** | 视频帧 → VLM caption → timeline card | 上述记忆系统均为纯文本 memory |
| **skill 路由** | lexical + semantic 两阶段，parallel fan-out 支持复合意图 | SkillRouter 覆盖路由，但无 egocentric context |
| **cascaded pipeline 透明性** | 完整文本推理链，支持 tool call + memory 注入 | Moshi / Mini-Omni 端到端推理链不透明 |

### 推荐 Benchmark 评测方案

| 评测维度 | 推荐 Benchmark |
|---|---|
| Egocentric 记忆检索 | **S-EMBER**（streaming，388h）、EgoMemReason |
| 跨 session 多轮记忆 | MemoryAgentBench、EgoLifeQA |
| 时间定位精度 | Ego4D NLQ（R@1, IoU=0.3/0.5）|
| Skill 路由准确率 | SkillRouter benchmark（~80K skills）|
| Tool 调用质量 | ToolBench / API-Bank |

---

*文档生成时间：2026-08-01 | 基于网络调研，建议在引用前核实论文最终发表信息*
