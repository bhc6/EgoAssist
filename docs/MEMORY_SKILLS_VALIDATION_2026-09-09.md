# 记忆延迟与 Skill 实测（2026-09-09）

已修复本地检索、Skill 资源加载、任务确认和参考工具执行问题。两种内置 Skill 的文本控制与真实 Qwen 音频链路可以演示；云端路由仍有延迟波动和错误标签，不能据此宣称完整眼镜产品已稳定完成。

## 实际发现与修复

- **记忆查询**：原“快速查找”扫描全部卡片后再开连接查键，可能把被 `kinds` 排除的卡片重新加入。现在从同一范围内的候选中按键排序；空类型集合不会扩大查询，原始 capture 不可召回，零分阈值也不能越过实体与时间约束。
- **检索成本**：去掉额外查键连接及随卡片数增长的 FTS `IN` 参数列表；固定 FTS 驱动连接顺序，避免 SQLite 选出重复 MATCH 的慢计划；不再为硬约束不匹配的卡片分词。仍保留候选审计，仍为线性扫描，不声称已经实现常数时间索引检索。
- **延迟可观测性**：SQLite 返回 `timing`，真实日志新增 `retrievalMs` 和 `writePolicyMs`，后者包括草稿检查、时间解释与写入策略，不等于纯 SQL 耗时。
- **Skill 参考资料**：过去只注入“请读取 references”的文字，实际没有读工具。新增独立 `skill_resources.py`，从清单加载小型 Markdown 政策，在 Skill 激活后注入正文及 SHA-256；校验路径、文件类型与总大小。不下载链接中的完整官方手册。
- **步骤一致性**：实测明确完成确认被漏判，模型口头进入下一步但后台未变。路由 v8 明确区分完成声明与问题；无已接受转换时，回复指令绑定当前后台检查点。确认、暂停、恢复、回退实测通过。
- **参考工具**：实测模型说“正在打开”，却没有调用工具。新增 `show-reference` 语义动作和独立 `routed_tools.py`：通过当前可用工具和会话权限执行本地解析，先持久化调用/结果，再回复；测试必须取得实际 PDF。原生模型 function calling 的稳定性不因此被视为通过。
- **误激活**：真实模型把取物说明写作误判为厨房/装配任务。内置包现在要求语义提议与清单领域证据一致才自动激活；审计保留被拒提议。新会话记忆实测已验证错误标签未改变活动 Skill。
- **多任务歧义**：多个任务及别名同时出现时，集合去重后判定歧义，不会被最后一个别名重新选中。

自动激活门槛的代价：未匹配已声明领域短语/任务名称的开放表达可能保持通用模式。仍可显式选择 Skill、已有任务或创建自定义任务；新领域应补充包清单并验证，不应无限放宽全局路由。

## 记忆到底慢在哪里

最新完整实测 `memory-3.json`，调用真实 `qwen-flash` 和 `qwen3.5-omni-flash-realtime`，使用测试生成的随机六位编号：

| 阶段 | 实测 |
|---|---:|
| 写入策略及本地持久化 | 13.095 ms |
| 从发送保存请求到文本保存回执 | 2338.4 ms |
| 保存请求的云端路由 | 2170.2 ms |
| 新会话检索 | 14.006 ms |
| 新会话查询的云端路由 | 2261.3 ms |
| 新会话查询首音频 | 3186.7 ms |
| 使用编号写取物说明的首音频 | 2424.2 ms |

验证要求：写入有 committed 卡片 ID；关闭写入连接后创建独立会话；不复制原对话或编号到新会话；召回审计命中已提交卡片；最终文本包含该随机值，并收到真实音频和 `response.done`；后续使用不误激活 Skill。取物说明只生成文本，没有发送给他人。保存回执目前只有文本。

因此，少量记忆的等待主要来自云端语义调用与音频生成。新增 API Key 不是本次必需条件。继续提速应优先测路由服务的首请求/连接等待和生成耗时；本次没有通过放宽 3 秒截止时间、后台残留重试或提前宣称“已保存”来掩盖等待。

本地规模实验为**合成数据**，不是公开视频数据集评测。相同 SQLite 文件上交替执行旧版 `f136c9d` 与新版各 30 次，查询与结果核对一致，去掉各自第一次后取中位数：

| 卡片数 | 旧版检索 | 新版检索 | 降幅 |
|---|---:|---:|---:|
| 100 | 6.675 ms | 4.881 ms | 26.9% |
| 1000 | 27.141 ms | 18.761 ms | 30.9% |
| 5000 | 311.466 ms | 216.706 ms | 30.4% |

Windows 实测会随机器负载波动：早期单独运行的 5000 卡基线约 118 ms、新版约 89 ms，后续独立运行又达到约 214 ms。只使用上述同文件交替比较支持相对改善，不把某个最好样本当作稳定延迟承诺。

## Skill 能演示到哪一步

`skills-7.json` 的 8 个真实回合全部收到完整音频、最终转写和成功结束事件，并核对后台检查点：

| 场景 | 验证结果 | 首音频 |
|---|---|---:|
| 番茄炒蛋首次激活 | kitchen-coach / prepare | 3880.0 ms |
| 重复、暂停、恢复 | 步骤不前移，paused 正确改变 | 1561.1–1644.2 ms |
| 用户明确完成、回退 | prepare → wash → prepare | 1680.5–1752.7 ms |
| 切换组装凳子 | assembly-coach / inventory | 1971.0 ms |
| 展示参考资料 | show_reference=resolved，受授权 PDF 实际读取成功 | 1722.0 ms |

步骤完成是测试用户声明，不是相机自动验证。首次 Skill 路由本次接近 3 秒截止时间（2980.2 ms）；早前运行也确实发生过超时。没有隐去失败后声称“全程零超时”。

当前有两个 Skill、五个内置任务。本次真实逐步验收覆盖番茄炒蛋和组装凳子；水果沙拉、燕麦粥、净水器的加载与控制由本地回归覆盖，不等于已真实做完三项物理任务。计时器在未接通持久工作流时仍为 unsupported，人工求助仍受连接器状态与确认约束。日报/周报/月报、真实眼镜采集、回声与打断、连续视觉完成判定均不在本次通过范围内。

## 模型比较与失败证据

探索性比较中，qwen-turbo 26 次中 20 次满足严格原始标签断言，中位数 1291.7 ms；较早 qwen-flash 的 24 次中位数 742.0 ms。两轮提示词和用例版本有少量变化，不作为严格模型排行榜。未观察到换用 turbo 的收益，继续保留 flash。

所有运行的状态保存在 `validation/2026-09-09-memory-skills/runs.json`：

- skills-1：验收脚本误读任务嵌套字段；skills-2、skills-5：供应商路由超时，脚本早期又未处理空 Skill。
- skills-3：实际步骤完成漏判，后台/口头回答不一致。
- skills-4：任务状态通过，但当时脚本没有断言参考工具执行，不能作为工具通过证据。
- skills-6：严格工具断言失败，实际没有工具调用。
- skills-7：修复后任务及真实参考工具通过。
- memory-1：写入/召回通过，但未检查无关 Skill；memory-2：更严格检查发现误激活；memory-3：激活门槛修复后完整通过。日志仍保留模型错误提议。

原始路由测试对“还没完成，不要进入下一步”曾断言无控制；返回 pause-task 是合理解释，后来修正了该用例断言。单纯“还没完成”的输入仍独立检查。没有修改历史结果文件。

## DeepSeek Harness 参考范围

官方仓库固定参考版本：[`deepseek-ai/deepseek-harness@c389f96`](https://github.com/deepseek-ai/deepseek-harness/tree/c389f96bf3a9b6807cb71ed6bdad5849be0df6d8)。

- [Skills 子系统](https://github.com/deepseek-ai/deepseek-harness/blob/c389f96bf3a9b6807cb71ed6bdad5849be0df6d8/docs/subsystems/skills.md)：目录摘要与完整内容分离、资源基址、按需读取。这里采用显式资源清单与活动上下文，不引入整套插件运行时。
- [Memory MCP 指南](https://github.com/deepseek-ai/deepseek-harness/blob/c389f96bf3a9b6807cb71ed6bdad5849be0df6d8/docs/user/guide/mcp-memory.md)：第三方记忆默认关闭；Harness 本身不替用户创建数据库或自动生成 embedding。借鉴其“写入 → 新会话召回 → 使用”验收方式。
- [架构](https://github.com/deepseek-ai/deepseek-harness/blob/c389f96bf3a9b6807cb71ed6bdad5849be0df6d8/docs/architecture.md)：工具服务与追加会话事件分工。这里让资料执行结果可审计，避免把自然语言承诺当成工具完成。

没有安装或运行上游 Harness，也没有宣称它提供了已验证的记忆加速算法。

## 复现与运行

在项目根目录使用 `.venv\Scripts\python.exe`：

```powershell
.venv\Scripts\python.exe -m pytest -o cache_dir=outputs/pytest-cache-harness
.venv\Scripts\python.exe scripts/verify_real_skills.py --output outputs/new-skills-run
.venv\Scripts\python.exe scripts/verify_real_memory.py --output outputs/new-memory-run
.venv\Scripts\python.exe scripts/benchmark_timeline.py --output outputs/new-memory-benchmark
.venv\Scripts\python.exe scripts/benchmark_timeline.py --output outputs/new-paired-benchmark --fixtures outputs/new-memory-benchmark --compare-ref f136c9d
```

输出目录必须尚不存在；真实测试需要已有 `QWEN_API_KEY` 和可用外联，失败返回非零。回归共 **349 项通过**，剩余为 aiohttp 的已知键类型/超时参数弃用警告。更新后的实际服务运行在 `http://127.0.0.1:8780`，另经真实装配 Skill 检查成功，会话 `3daa6f0ee9d84dd3`；服务日志在 `outputs/demo-harness-8780.*.log`。

所有延迟均为文本控制输入至客户端收到数据，未包含眼镜麦克风 ASR 或真实扬声器播放时间。证据目录仅包含隔离测试数据与筛选后的日志，未提交 API Key、音频或运行数据库。
