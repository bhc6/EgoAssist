# A/C 完整 500 题：数据准备与论文对齐

状态：完整数据已准备并校验，A/C 全量回答尚未启动。用户要求“完全对齐”，需要明确是 A/C 之间的受控对照，还是同时复现论文的模型与流程；当前工作区只有 Qwen 访问配置，没有 GPT-4o/GPT-4o-mini 的访问配置。

## 已完成的数据准备

- 使用原有固定 revision `143fb319be7aa5ae210c936bf4f0f3a86092afb0` 的官方 JAKE 500 题，未做类别、日期或目标时间格式筛选。
- 五类题数：EntityLog 125、EventRecall 126、HabitInsight 61、RelationMap 125、TaskMaster 63，与官方完整 JAKE 文件一致。
- DAY1-DAY7 共 150 份官方 SRT、128,364 条来源，其中描述 71,724 条、转写 56,640 条。新增获取 DAY1 的 20 份文件，其余复用并校验已有源文件哈希。
- 问题与选项存于公开查询文件；正确选项、类别和目标时间单独存放。列表、跨天和不规则目标时间标注原样保留，不能因为目标定位指标无法解析就删除对应选择题。
- 验证了全部 500 个题目 ID、目标标注原样保留、公开问题字段隔离以及冻结文件哈希；准备阶段模型调用为 0。
- 已暴露的 30 题以及更早 DAY1 开发数据不重新标为独立测试；其他题目的历史语义重叠尚未审计。

[数据统计](../artifacts/egolife-jake-full500-freeze-20260914/selection.json)、[冻结清单](../artifacts/egolife-jake-full500-freeze-20260914/freeze.json)、[准备协议](../artifacts/egolife-jake-full500-freeze-20260914/protocol.json)。源文件清单引用本地缓存的绝对路径，数据不保证随仓库克隆获得。

## 对齐边界

原论文来源：[EgoLife v1 第 5 节及表 5](https://arxiv.org/html/2503.03803v1)。该版本使用 JAKE 500 题快速评测，将不同模型接入 EgoButler 生成描述，统一使用 GPT-4o 回答；文本相似度检索 top-3 的 30 秒片段，并使用 GPT-4o-mini 重新查询。

| 条件 | 当前已具备 | 仍需确定或补齐 |
| --- | --- | --- |
| 500 题与类别分布 | 全量冻结，0 题排除 | 无 |
| 七天历史 | 官方描述/转写完整 | 不等同于论文某个模型生成的描述库 |
| A/C 共同输入 | 可使用相同问题、选项、历史与硬约束 | 固定最终运行协议 |
| 回答模型 | qwen-flash 可用 | 论文使用 GPT-4o，当前缺少访问配置 |
| 重新查询模型 | 尚未执行 | 论文使用 GPT-4o-mini，当前缺少访问配置 |
| 检索单元与预算 | 既有 A/C 为 SRT 来源 top-5、256 条扫描预算 | 论文为 top-3 的 30 秒片段；不可悄悄替换后仍称原 A/C |
| 时间精度 | 当前适配器将 HHMMSSxx 向下取整到秒 | 与论文时间实现的等价性尚未证明 |
| 官方关键词 | 未进入公开查询文件 | 公开代码部分路径会读取数据集 keywords，需处理输入隔离差异 |
| 请求失败 | 保留失败并可重试，不计作随机答题 | 公开代码存在失败时随机选项的分支，不采用该失败计分方式 |

公开代码核查来源：[EgoRAG README](https://github.com/EvolvingLMMs-Lab/EgoLife/blob/main/EgoRAG/README.md)、[RagAgent.py](https://github.com/EvolvingLMMs-Lab/EgoLife/blob/main/EgoRAG/egorag/agents/RagAgent.py)。README 声明需要 `keywords`，`parse_query` 读取该字段，部分检索路径使用它；另有从问题生成关键词的路径，不能将二者混为一谈。公开主分支也不自动等同于论文运行时快照。

沿用 qwen-flash 的完整 A/C 实验，可以对齐题目覆盖和两组运行条件，但应标注为受控记忆机制对照，不能称为论文完整复现。严格论文模型版本需要先补齐模型访问，再冻结输入材料、片段粒度、提示词和重新查询流程。任何版本都不得使用正确选项、目标时刻或目标来源参与检索。

## 准备入口

```powershell
.\.venv\Scripts\python.exe scripts/prepare_egolife_full.py --previous artifacts/egolife-independent-freeze-20260914-r4 --output artifacts/new-egolife-jake-full500-freeze
```

该命令只准备数据，不运行 A/C 答题。已经存在的输出目录不会覆盖。完整 A/C 运行结果尚不存在，不得把之前 30 题的准确率填入全量结果。
