# Prompt Assets

所有运行模型的自然语言策略在本目录或 `egoassist/skills/*/SKILL.md`、该 Skill 的 `references/*.md` 中维护。代码负责结构、时钟、用户范围、执行权限和证据校验，不在 Python 里维护长篇模型指令。

| 文件 | 用途 |
| --- | --- |
| `router_protocol.json` | 单次语义路由，记忆写入/查询候选、Skill 和任务控制 |
| `interaction.json` | 对话对象、输出权限、取消核验、原答续播与播放证据约束 |
| `answer_completion.json` | 原问题后台文字补答，不重复工具操作 |
| `run_egoassist.json` | 每轮回复的证据、执行回执、任务和范围约束 |
| `skills.json`、`skill_package.json`、`task.json` | 通用助手及加载 Skill 后的上下文组合 |
| `perception.json`、`omni.json` | 实际图像观察、主动视觉提示 |
| `insights.json` | 日报、周报、月报和有来源的主动建议 |
| `locale_zh.json`、`locale_en.json` | 语种策略、回复指令及相关界面消息 |
| `real.json` | 离线数据集问答与图像描述后端 |

保留 `{v0}` 等模板参数；它们对应调用点的具名输入顺序。编辑模板后重启服务并新建模型会话，确保模块级策略与当前会话都更新。没有浏览器远程写入提示词的接口。

前端“提示词与真实请求”显示模板路径、SHA256，以及本用户当前案例中实际发送的路由 messages、session.update 指令、response.create 指令、检索注入和报告输入/原始输出。模板目录表示当前文件；历史请求表示当时实际使用的文本，两者不能混为一谈。审计不展示或推测隐藏思维链。

审计支持按游标加载全部历史，包含工具各轮消息/回执、视觉原始输出和后台记忆恢复。展开状态在轮询中保留，模型与预算显示实际配置。自然保存、ASR 分段草稿续写和未知声音的交互约束分别在 router_protocol.json 与 scene.json 调整；不能通过增加领域关键词修补模型误判。

调参验证：先跑受影响的行为测试，再运行 `scripts/verify_real_social_memory.py` 与 `scripts/verify_browser.py --real-turn`。保留每轮失败结果，比较召回正确性、来源完整性、错误执行率与实测延迟。带示例的提示词是可修改的少样本协议说明，不是固定答案查询表。

打断控制使用 `scripts/verify_real_interaction.py`、`scripts/verify_real_voice_stop.py` 和 `scripts/verify_real_voice_stop_browser.py --local-vad`，分别覆盖应用控制、真实合成语音 ASR 和浏览器播放。`research_interruption_semantics.py` 仅用于语义探测，不能替代产品验收。取消核验和分类重试的实际请求同样保存在提示词审计中；模型自报置信度不是执行授权。
