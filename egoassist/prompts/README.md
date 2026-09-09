# Prompt Assets

所有运行模型的自然语言策略在本目录或 `egoassist/skills/*/SKILL.md`、该 Skill 的 `references/*.md` 中维护。代码负责结构、时钟、用户范围、执行权限和证据校验，不在 Python 里维护长篇模型指令。

| 文件 | 用途 |
| --- | --- |
| `router_protocol.json` | 单次语义路由，记忆写入/查询候选、Skill 和任务控制 |
| `run_egoassist.json` | 每轮回复的证据、执行回执、任务和范围约束 |
| `skills.json`、`skill_package.json`、`task.json` | 通用助手及加载 Skill 后的上下文组合 |
| `perception.json`、`omni.json` | 实际图像观察、主动视觉提示 |
| `insights.json` | 日报、周报、月报和有来源的主动建议 |
| `locale_zh.json`、`locale_en.json` | 语种策略、回复指令及相关界面消息 |
| `real.json` | 离线数据集问答与图像描述后端 |

保留 `{v0}` 等模板参数；它们对应调用点的具名输入顺序。编辑模板后重启服务并新建模型会话，确保模块级策略与当前会话都更新。没有浏览器远程写入提示词的接口。

前端“提示词与真实请求”显示模板路径、SHA256，以及本用户当前案例中实际发送的路由 messages、session.update 指令、response.create 指令、检索注入和报告输入/原始输出。模板目录表示当前文件；历史请求表示当时实际使用的文本，两者不能混为一谈。审计不展示或推测隐藏思维链。

调参验证：先跑受影响的行为测试，再运行 `scripts/verify_real_social_memory.py` 与 `scripts/verify_browser.py --real-turn`。保留每轮失败结果，比较召回正确性、来源完整性、错误执行率与实测延迟。带示例的提示词是可修改的少样本协议说明，不是固定答案查询表。
