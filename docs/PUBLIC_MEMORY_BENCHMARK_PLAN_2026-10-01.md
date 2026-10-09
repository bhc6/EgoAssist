# 2026 顶会对接：公开记忆基准与按比例小测试集

日期：2026-10-01。状态：**官方元数据已清点，300 题抽样清单已生成；未冻结、未运行模型、无新分数。**

本补充按最新讨论将公开基准置于主实验位置。[机制归因协议](MEMORY_MECHANISM_CAUSAL_PROTOCOL_2026-10-01.md) 的同信息、同候选、G×V、结构反证、预算及成本要求继续适用；其旧 8,640 次矩阵不再作为默认启动顺序。自建变化轨迹改为机制诊断。**若研究定位包含智能眼镜/第一视角记忆，EgoLife 类真实经历评测是正式实验轨，不能只留作可选演示。**现有 Mini-300 是已生成的文本任务子集，第一视角选题另行审计，尚未生成。新的数据、阶段和主比较以本文及 [公共基准登记](PUBLIC_MEMORY_BENCHMARK_REGISTRY_2026-10-01.json) 为准。

## 1. 选择依据：2026 论文实际使用，不能只看数据集年份

| 公开基准/对接工作 | 已核验的依据 | 本项目角色 |
| --- | --- | --- |
| LongMemEval-S cleaned | LightMem 官方仓库标为 ICLR 2026，并提供该数据的实验入口；LongMemEval 本身是 ICLR 2025 工作 | 主基准：跨会话、多证据、时间、更新、偏好与拒答 |
| MemoryAgentBench | 官方仓库引用 `hu2026evaluating`，会议为 ICLR 2026，OpenReview `DT7JyQC3MR` | 主基准：检索、测试时学习、长程理解、冲突处理 |
| LoCoMo | LightMem（ICLR 2026）与官方仓库标为 ACL 2026 的 StructMem 均提供 LoCoMo 实验入口 | 已暴露开发/复现集；不冒充新独立测试 |
| StructMem | 作者仓库标为 ACL 2026；其事件绑定、跨事件连接与我们的 G 假设接近 | 必须考虑的近邻方法；先核验原生支持，再作明确标注的迁移 adapter |
| EgoLife / EgoLifeQA | CVPR 2025 官方论文与项目，面向跨小时/跨天的第一视角生活问答；2026-10-01 官方 QA 目录仅列 JAKE 文件 | 智能眼镜/第一视角论文的正式场景轨，分共享感知和原始媒体条件；不能假设已有多人 QA |
| Ego4D Episodic Memory | 官方 NLQ/VQ/MQ 定义；NLQ 仍列入 2026 CVPR EgoVis workshop challenge | 可补充时间片段/物体找回能力；定位指标单列，不作为同口径 QA 总分 |
| LoCoMo-Plus、LongMemEval-V2 | 新能力扩展，具体协议/暴露/数据尚需逐项核验 | 暂不扩大首轮主表；不因名称新就代替成熟基准 |

“2026 顶会都在用”应理解为有代表性的已接收工作采用，而非每篇论文都使用相同数据。上表会议状态依据作者官方仓库与论文引用；不将 arXiv 发布自动等同顶会接收。

**需要修正的创新边界：**StructMem 已研究事件绑定与跨事件连接。我们的研究不能停在“记忆有事件结构”，应检验相同信息下的集合互补目标、版本兼容约束及其失败边界。论文对照不能只放一个弱平面检索器。

## 2. 已生成的 Mini-300：两套分开评分，不合成总准确率

### 2.1 LongMemEval 100 / 500

元数据来自官方 cleaned release 的 `longmemeval_oracle.json`，仅提取 question ID、type 和拒答标志；没有把 oracle 的支持历史当作测试输入。实际运行仍须下载、校验完整 `longmemeval_s_cleaned.json`。

固定数据 revision：`98d7416c24c778c2fee6e6f3006e7a073259d48f`。

| 官方 question_type | 全量题数 | Mini 题数 |
| --- | ---: | ---: |
| temporal-reasoning | 133 | 26 |
| multi-session | 133 | 27 |
| knowledge-update | 78 | 16 |
| single-session-user | 70 | 14 |
| single-session-assistant | 56 | 11 |
| single-session-preference | 30 | 6 |
| **合计** | **500** | **100** |

拒答不是第七个互斥类型：官方以 ID 的 `_abs` 后缀标记。全量 30 题，小集保留 6 题，分别来自更新 1、跨会话 3、用户事实 1、时间推理 1。抽样按 `question_type × abstention` 联合分层；用最大余数法处理整数配额，故不是机械地对每类四舍五入。

剩余 400 个 ID 已单列。要维护论文全量结果的解释性，Mini 的用途是**开发结束后的盲测第一阶段**：跑完不调方法，才可接着跑剩余 400 题并还原 500 题完整结果。若拿 Mini 反复调参，须改标为开发集；剩余 400 才是新 holdout，500 题整表需披露测试调参，不能继续称全量独立测试。

### 2.2 MemoryAgentBench 200 / 3,671

已读取官方固定 revision 的四个 Parquet，仅将抽样所需元数据、问题哈希、索引及原始上下文哈希写入 inventory；无答案或问题正文进入抽样脚本。

数据 revision：`7ea066982b140a19337e17e60d45d4076e042faf`。

| 官方能力 split | 原始历史行数 | 实际问答数 | 按原始问答比例的 Mini |
| --- | ---: | ---: | ---: |
| Accurate_Retrieval | 22 | 2,000 | 109 |
| Test_Time_Learning | 6 | 700 | 38 |
| Long_Range_Understanding | 110 | 171 | 9 |
| Conflict_Resolution | 8 | 800 | 44 |
| **合计** | **146** | **3,671** | **200** |

先按能力问答数分配 200 的配额，再在能力内按每个官方 source/长度配置的问答数分配。**22 个 source/长度配置全部覆盖**。历史行数不是题数，不能按 146 行随手抽 20 行就说保持了任务比例。

完整子配置配额见抽样 manifest。例如 FactConsolidation 保留 SH/MH 与 6k/32k/64k/262k 全部八种配置，不能只选最短历史；长程理解保留摘要 5、DetectiveQA 4。9 个长程理解问题只够发现大问题，不足以宣称这一能力显著领先。若后续重点研究它，应在本轮解封前另立加密度子集并单列报告，不能看错题后临时补题到显著。

200 个问答涉及 45 条不同原始历史，共 42,920,486 个原文字符；这是本轮清点的字符数，不是 tokenizer token 数。**少抽问题不会同比减少长记忆建库成本。**使用本地已有免费缓存和研究共享产物可以节约实际执行，但部署成本账仍需计入各方法真正需要的建库/更新。

### 2.3 不改变任务难度的抽样规则

- 不截断原历史、不仅保留 gold 附近窗口、不替换长流为摘要；保留官方增量顺序与待核定的 chunk 策略。
- 不根据正确答案、模型难度、历史分数或“是否有利于我们”选题。
- seed=`20261001`；配额使用精确分数的最大余数法，同余数按字典序；配额内按 `SHA256(seed|revision|stratum|sample_id)` 排序取样。
- 每个样本保留原始 ID 或 `(split,row_index,question_index)`、上下文哈希、实际入选概率和分层信息。
- 方法之间使用同一选题 manifest。模型/成本/统计/暴露审计完成前，这只是**选题草案**，不是已经完成的预注册。

## 3. 不能遗漏的重叠与统计问题

MemoryAgentBench 的 `longmemeval_s*` 是 300 问、重新组织历史的条件，**不是**官方 500 问 LongMemEval-S。Mini 中选出的 16 道 S* 问题都可在全量 LongMemEval 找到 ID，其中 5 道同时在 LongMemEval Mini-100。

保留重叠才能忠实反映各自公开协议，但必须：

1. 不把两个结果当成独立复现，不把 300 个选择记录叫作 300 个独立问题；相关问题和来源纳入分组统计。
2. MAB 报完整官方分项；另外提供“去除 S* 的非重叠诊断”，不把该诊断冒称官方全表。
3. EventQA 不同长度、FactConsolidation 多长度/同家族、同一历史中的多个问题也存在相关性。45 条历史不保证 45 个独立生成根；family/近重复审计尚未完成。
4. 小集恢复总体比例时，对可合并的同一指标用 `Σ_h (N_h/N) × mean_h`；保存实际 `n_h/N_h`。不要把摘要 F1、推荐 Recall@5 和精确匹配平均成一个“总准确率”。
5. 官方若采用任务宏平均，按官方任务权重恢复，不能用我们的问答微平均替换。不同指标分表；每个配置只抽 4–27 问时区间会很宽。

## 4. “能直接和论文比较”的三个级别

| 比较方式 | 可以做什么 | 不可以做什么 |
| --- | --- | --- |
| 同小集、同条件复跑公开方法 | 对 Mini-300 中相同问题作配对比较 | 把自己的 Mini 分数减去论文全量分数，称涨点 |
| 同全量、同官方评分，但 reader/输入策略不同 | 比较完整系统配置，列清差异 | 将差值完全归因于记忆机制 |
| 同 release、完整子任务、消息范围、reader、提示词与评分设置匹配 | 复现论文对应表/列，报告偏差与我们的方法 | 只因数据集同名就视为完全可比 |

小集可以用于估计总体表现，但区间/权重/抽样误差不能省略。若公开了论文方法的逐题预测，可在核验版本、模型与 ID 对齐后取同一子集；没有逐题结果则实际复跑，不臆造子集基线。

### 特别核对到的 LightMem 输入差异

官方 `experiments/longmemeval/readme.md` 写明，其论文 Table 2 的 LongMemEval 条件使用 **`messages_use="user_only"`**；方法描述中的 user/model 成对数据并不意味着实际索引双方消息。

因此分两张表：

- **机制与统一条件表：**我们、强平面基线及适配的公开方法都使用同一消息范围。若选择完整双方对话，必须说明 LightMem 是 full-message adapted recipe。
- **论文原设置复现表：**匹配 Table 2 的 user_only、模型、预算和评分；不能让我们额外看到助手原文，再把优势归因于 G/V。

StructMem 官方入口目前明确提供 LoCoMo 实验。迁移到 LongMemEval/MAB 时要标为 adapter，并先检查原生/迁移行为，不能直接写成已发表的该数据集结果。LoCoMo 本地已经暴露，只承担开发和公开复现角色。

## 5. 官方指标保持原样，机制指标另列

LongMemEval：官方 QA correctness 及题型分解，使用固定版本的 `evaluate_qa.py`/`print_qa_metrics.py`，记录 judge 完整版本和提示词。现代替代 judge 的分数不能直接当成论文原 judge 分数。

MemoryAgentBench 官方 README 对指标明确区分：

| source | 官方指标 |
| --- | --- |
| EventQA、RULER QA1/QA2、FactConsolidation SH/MH | substring exact match |
| DetectiveQA、ICL 分类系列 | strict exact match |
| 推荐 recsys | Recall@5 |
| LongMemEval-S* | LLM judge |
| InfBench 摘要 | 遵循 HELMET 的 LLM judge F1 |

作者提示严格解析可能把带前缀的标签判错，但要复现原表，就不能只替我们放宽解析。需要宽松解析时另加统一的诊断列。

引用完整率、有效性一致、支持联合率、陈旧答案和总成本仍按机制协议收集，用来解释分数；不修改官方分数定义。引入引用输出格式的额外要求也可能影响原生 QA，因此 native-recipe 表和 citation-enriched 机制表分别登记 prompt。

## 6. 调整后的首轮执行与对照

### 开发阶段

只用已暴露 LoCoMo/EgoLife 和独立开发轨迹。复用原 P0 校准目标，验证 reader/scorer、候选及 G/V 开关；**Mini-300 不承担反复调参**。先完成抽样/输入/分组审计和预算核算，再冻结。

### A：公共小集，最多 1,400 次回答

- Mini-300 上跑 M00/M10/M01/M11 四组：1,200 次。
- LongMemEval Mini-100 上补 LightMem 与经校准的 StructMem adapter：200 次。无法可靠适配时记为未完成，不运行坏基线制造优势；另保留 LoCoMo 原生复现记录。
- 分项报告各类分数、支持证据和全成本；同输入预算及长度控制原则继续适用。
- 不因只在某几个子任务涨点，就改变下一阶段的预登记主表。

### B：论文对应完整任务，方法不变再扩展

- LongMemEval-S 完整 500 问：M00、M10、LightMem、StructMem adapter 共 2,000 次条件回答；原生复现另按确实匹配的配置计账。
- MemoryAgentBench **完整 Conflict_Resolution split 的 800 问/八个配置**：四个 G×V 条件共 3,200 次条件回答。这可对接论文冲突处理相应列，不能宣称跑完全部 MemoryAgentBench。
- A/B 中完全相同的请求及状态可复用，避免重复付费。A 的其他 MAB 能力 156×4=624，以及 LongMemEval 的 M01/M11 小集 100×2=200，是额外诊断。
- A 加 B 共最多 **6,024 次不同条件回答**，即 A 的 1,400 加新 4,624；不含开发 P0、抽取/建库/judge/重试、额外原生复现或后续机制反证调用。B 只在数据、adapter、功效/区间和费用冻结后启动。

模型漂移、请求 hash 不同或状态前缀不同不能强行复用。若 Mini 结果后修改了方法，应开启新研究版本和新的未见测试；不能把改前与改后预测拼成一个“全量分数”。

### C：机制证据与外部有效性

结构打乱、同池邻接、长度耦合与完整支持 oracle 仍必不可少；复用已定义子集规则，额外调用在运行前登记。自建最小对和局部修复用于补充“为什么有效”。第一视角正式轨见下面 D；只展示文本公共榜单涨点不足以支持智能眼镜场景的机制结论。

新版确认性比较拟为：G 在完整 LongMemEval 上 M10−M00；V 在完整 Conflict_Resolution 上 M11−M10，各官方配置等权（当前每配置 100 问），与旧协议一起在开跑前冻结并做 family-wise 控制。Conflict_Resolution 只有八条公开历史，不能以 800 为独立样本数计算很窄区间；对未知流的泛化仍需要多个独立变化轨迹或更多外部数据。

### D：第一视角正式轨——目标 100 题，两种输入条件

**当前状态：设计完成，数据/暴露审计及 ID 清单未完成；不能将其称为已经建好的 Mini-400。**原 Mini-300 保留。新增目标为 100 个未用于本项目调参的 EgoLife 官方问题，在两种输入条件各运行 M00/M10，最多新增 100×2×2=400 次最终回答。原始媒体感知、embedding、建库与评分另计。A 阶段若同时加入 D，计划最多 1,800 次回答；A+B+D 最多 6,424 次，均以数据和配置冻结为前提。

| 条件 | 各方法获得的输入 | 固定条件 | 支持的结论 |
| --- | --- | --- | --- |
| D-memory：共享感知结果 | 同一佩戴者在提问前可获得的 caption/ASR、时间/人物线索，使用一份冻结缓存 | 同抽取信息、候选、reader、预算；只变 G | 第一视角事件流中的记忆组织增量，不证明完整视觉/声音感知能力 |
| D-media：原始第一视角视频/音频 | 同一历史媒体前缀与相同采样政策 | 同感知模型、采样率、图像分辨率、ASR、身份信息来源、reader 和资源限制 | 感知到记忆再到回答的实际表现；仍需区分感知与记忆损失 |

两个条件使用相同 query IDs，结果分表。官方标注通常更干净，不能将标注轨直接称为实际眼镜性能；若标注使用其他人的音轨/第三视角资料，必须限定到佩戴者可感知范围，无法限定的另标 privileged-annotation 诊断。不能通过真值帧、正确选项、提问后的画面或全体人物 oracle 生成专属感知缓存。

D-media 的感知预处理应在问题无关的完整过去流上产生；如查询时允许回看历史原始媒体，所有组使用相同访问权限和帧/音频预算，额外感知费用完整计入。不同机制若还改变了原始媒体提名范围，应单列整体方法比较，不能继续称“固定候选池”的纯 G 消融。

**需要检验的第一视角特有问题：**人物—行为归属、同一动作多帧去重、先后与上一次事件、物体位置变化、跨事件关联、观察缺口与不可确定范围。按官方已有类型/日期比例抽样；官方无某种标签时先盲标作诊断，不制造“官方包含该类别”的说法。尤其不能把镜头未看见等同事件未发生，不能用少量片段回答全天精确次数。

数据边界：截至 2026-10-01，官方 `EgoLifeQA` 目录只有 `EgoLifeQA_A1_JAKE.json`。项目覆盖六位参与者，不等于六人均有公开 QA。此前旧 30 题、110 题及其相邻/近重复事件只用于开发或回归。只有单人剩余未见问题时，可做同人跨日/跨事件测试并明确范围；跨人物泛化需要额外有效标注，不能靠同一人的多个日期替代。目标 100 题不足时如实登记，不拿已暴露题补数。

可以补一个小规模 Ego4D NLQ 验证：它要求找回回答所对应的时间窗口；VQ 针对物体最后出现的位置。按官方 split/视频划分与官方定位评分执行，作为证据找回的独立支撑，不把定位成功等同完整问答成功。是否加入、样本数和预算须在运行前登记，本轮未下载或抽样 Ego4D。

报告官方 EgoLife QA 分数，以及有效支持完整率、人物/时间错误、事件重复计数、不可回答误答、每小时媒体建库成本、单位历史存储和读取成本。官方未覆盖的计数/拒答任务只作额外诊断，分开标明标签来源。只答对选项而缺乏可见证据不能支持机制解释。

若论文以第一视角为主要主张，应在测试前将 D-memory 的 M10−M00 官方 QA 差异列为第三个确认性端点，与 G/V 共同控制多重比较；D-media 保持系统验证，预登记预算与失败指标。100 个问题并不保证足够功效，更不等于 100 个独立人物。原始媒体轨若尚未完成，论文须限制为第一视角描述流的记忆研究，不能宣称真实智能眼镜端到端能力。

## 7. 已完成的产物、验证与仍缺的工作

已完成：官方 LongMemEval 500 题类型元数据、MAB 四个固定 revision Parquet 的 146 行/3,671 问清点、22 配置配额、300 个确定性 ID 清单、跨数据集直接 ID 重叠审计、14 项抽样/不变性检查。模型调用为 0。

产物：

- [抽样脚本](../scripts/prepare_memory_benchmark_pilot.py)：仅标准库，接收元数据，不调用模型、不读取答案。
- [Mini-300 逐题清单](../outputs/benchmark-sampling-20261001/pilot-300-manifest.json)。
- [抽样校验](../outputs/benchmark-sampling-20261001/sampling-validation.json)。
- [LongMemEval inventory](../outputs/benchmark-sampling-20261001/longmemeval_inventory.json) 与 [MAB inventory](../outputs/benchmark-sampling-20261001/mab_inventory.json)。
- [官方来源及哈希](../outputs/benchmark-sampling-20261001/official_sources.json)。

`outputs/` 是本机研究产物，不随 Git 提交；原始资料和读取 Parquet 的临时依赖均留在此处，没有修改生产依赖文件。分享时优先提供许可允许的 ID、哈希、脚本和上游下载方式。

复现抽样（现有输出不可覆盖，请使用新文件名）：

```powershell
.\.venv\Scripts\python.exe scripts/prepare_memory_benchmark_pilot.py `
  --lme-inventory outputs/benchmark-sampling-20261001/longmemeval_inventory.json `
  --mab-inventory outputs/benchmark-sampling-20261001/mab_inventory.json `
  --output outputs/benchmark-sampling-20261001/pilot-300-reproduced.json
```

尚缺：完整 S 历史的 ID/文件哈希对照、本项目暴露/语义近重复审计、MAB 底层历史家族归并、reader/baseline/scorer/chunk 配置冻结、G/V 实现、真实成本与统计功效校准。没有因为已经生成 ID 清单就把状态标为 ready 或公布分数。

## 8. 官方来源

2026-10-01 已访问并保存来源内容 SHA256，具体见来源登记；可变 main 的脚本还须在正式运行前锁 commit。

- LightMem 官方：`https://github.com/zjunlp/LightMem`；LongMemEval 复现与 user_only 说明：`https://github.com/zjunlp/LightMem/blob/main/experiments/longmemeval/readme.md`。
- StructMem 作者实现：`https://github.com/zjunlp/LightMem/blob/main/StructMem.md`。
- MemoryAgentBench 官方：`https://github.com/HUST-AI-HYZ/MemoryAgentBench`；论文：`https://openreview.net/forum?id=DT7JyQC3MR`；数据：`https://huggingface.co/datasets/ai-hyz/MemoryAgentBench`。
- LongMemEval 官方：`https://github.com/xiaowu0162/LongMemEval`；清理版数据：`https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned`。
- EgoLife 官方项目：`https://egolife-ai.github.io/`；CVPR 2025 正文：`https://openaccess.thecvf.com/content/CVPR2025/papers/Yang_EgoLife_Towards_Egocentric_Life_Assistant_CVPR_2025_paper.pdf`；公开 QA 目录：`https://huggingface.co/datasets/lmms-lab/EgoLife/tree/main/EgoLifeQA`（2026-10-01 查看，仅列 JAKE QA）。
- Ego4D 官方情景记忆定义：`https://ego4d-data.org/docs/benchmarks/episodic-memory/`；2026 挑战：`https://ego4d-data.org/docs/challenge/`。Workshop challenge 与主会论文接收分别陈述。

这些材料支持基准选择、输入条件与指标定义；没有据此认定我们的新机制优于任何论文。
