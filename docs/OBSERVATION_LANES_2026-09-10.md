# 观察、交互与记忆的独立运行

日期：2026-09-10。由项目 `20362cd` 改造；原研究建议见[视频事件分割调研](STREAMING_VIDEO_EVENTS_RESEARCH_2026-09-10.md)。

## 这次实现

原实现的任务检查优先级会直接替代环境观察。现在复用现有 `_OmniPerceptionLane`，为开放观察配置独立连接和采样时钟，任务视觉检查及实时回答继续使用各自通道。两种观察都按需连接；没有画面时不主动调用视觉模型。任务期间关闭重复的通用场景描述，问题专用视觉理解仍保留。

每条观察通道最多一个在途请求和一个待处理最新帧，合并被覆盖的待处理帧时记录 `perception.frame.coalesced`。提交帧及其历史背景先进入审计日志，嵌套上下文深拷贝，防止排队期间内容被改变。单通道超时沿用 `EGOASSIST_PERCEPTION_TIMEOUT`。这不是跨供应商的全局优先级调度器；增加并行连接仍需监测账号限流和资源竞争。

`runtime.py::ThreeLaneRuntime` 向环境观察提供近期用户原话、当前案例的活动记忆卡片、待澄清记录和任务状态。数量与文本预算分别由 `EGOASSIST_OBSERVATION_REFERENCE_LIMIT` / `EGOASSIST_OBSERVATION_REFERENCE_CHARS` 控制，默认 4 条 / 1000 字符；截断有标记，完整原文仍留在日志。助手消息不作为用户背景输入。卡片带来源、类型及时间，不新增行为类别。

三链路现已作为顶层主框架：案例持有 `ThreeLaneRuntime`，接口分别提供 `runtimeLanes.observation / interaction / memory`。后台记忆状态不再寄存在 ambient 对象中，补答作业归入交互。模块归属和后续迁移边界见 [ARCHITECTURE.md](../ARCHITECTURE.md)。

主框架调整后再次真实验收：`outputs/runs/three-lane-framework-real-01` 通过，环境模型 2183.1 ms、任务模型 1034.5 ms、浏览器观察 2601.4 ms；问答首音频 2949 ms，完整回答 9184 ms。确认新接口、两路观察及回答的重叠执行，1440 / 390 布局和刷新恢复通过。最终全量回归 451 项通过，0 失败、0 跳过，证据 `outputs/three-lane-framework-final.xml`。

提示词 `perception/reference_context` 明确要求历史背景只影响关注点，不能证明当前画面存在某件事，也不能关闭无关变化的开放观察。提示词约束不等于事实正确率保证；模型仍可受到历史背景影响，需要持续做反事实与误报评估。

`SharedScene` 接收已接受的观察并为每个问题保存可用观察快照。未来帧被排除，后到结果不覆盖旧问题。文字问题即使当前相机没有新帧也能引用此前观察，且引用保留采集时间；语音问题使用已有的媒体时间绑定，无可靠时间边界时不附加观察。专门针对该问题原帧的视觉分析仍可补充结果。快照随 `scene.turn` 持久化，重启可恢复。

后台记忆恢复、报告、补答复用原有持久任务机制，没有再增加一套任务框架。前端新增三栏真实状态：现场观察、用户交互、后台记忆与报告。后台计数仅表示记忆恢复与报告作业，不将同步记忆写入冒充后台任务。

## 真实运行与修复

脚本：`scripts/verify_observation_lanes_real.py`。真实 DashScope Qwen，Edge，真实 EgoLife 视频 `DAY1_A1_JAKE_11100000.mp4` 的采样帧；无供应商 mock，无眼镜硬件。证据目录不提交 Git。

| 运行 | 结果 |
| --- | --- |
| `outputs/runs/observation-lanes-real-01` | 新状态栏 JS 字符串缺少结束引号，浏览器脚本未加载；已修复并保留失败记录 |
| `outputs/runs/observation-lanes-real-02` | 并行、记忆、问答及浏览器通过；日志复查发现任务 JSON 代码围栏解析失败，初版脚本检查不充分 |
| `outputs/runs/observation-lanes-real-03` | 修复解析并加强检查后通过；任务与环境观察重叠，环境观察与回答生成也重叠 |

修复前，任务视觉模型给出合法 JSON 并标注 `unrelated=true`，但外围含 Markdown 代码围栏。任务解析器误判 malformed，环境解析器却能接受。现在两者共享 JSON 对象解析，同时避免把字符串 `"false"` 用 Python `bool()` 误转成真。与任务无关的画面仍按既有规则拒绝用于推进任务，保留为开放环境观察。

第三轮测量：任务模型 1045.2 ms，环境模型 2403.5 ms，视频提交至浏览器显示环境观察 2633.9 ms；后续问题首文本 2014 ms、首音频 2318 ms、完整回答 8125 ms。完整回答包含较长场景描述与任务追问，不应宣传为短回复体验已经解决。第二轮环境模型 2445.1 ms；这些是少量样本，不是 p95 或硬件延迟。

原图哈希、模型请求/输出/usage、任务上下文、引用观察、写入记忆、问题绑定、刷新恢复和 1440 / 390 像素截图均归档在报告及会话 JSONL。真实任务背景为做菜，视频却是围桌场景；模型区分了二者，没有用环境观察自动推进做菜步骤。具体物体描述未做全面人工标注，不能据此宣称视觉识别准确率。

## 边界

- 本次实现是独立采样观察和跨链路背景引用，没有实现连续视频事件分割、多帧动作模型或全量录像。
- 刷新后观察记录与任务恢复；浏览器摄像头断开期间没有新媒体，界面显示等待连接。服务重启不恢复已消失的实时摄像头数据。
- 未完成帧的证据与请求可审计，但本次没有新增观察请求的持久重放队列。后台报告、记忆恢复和补答沿用已有恢复逻辑。
- 默认引用近期案例记忆，不是全库语义检索，也不自动跟踪人脸身份。历史观察是模型判断，不能当用户确认。
- 本轮真实验证未覆盖音频输入、日周月报告全部生成、长时间限流或实物眼镜功耗。相关旧验收不能替代新硬件验收。

## 复现

最终全量回归：450 项通过，0 失败、0 跳过；记录为 `outputs/observation-lanes-final.xml`。另通过 JavaScript 语法检查及 `git diff --check`。已有 aiohttp AppKey 与 timeout 弃用警告仍存在，不影响本次测试结果。

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe scripts/verify_observation_lanes_real.py --output outputs/runs/observation-lanes-new-run
```

真实脚本要求已配置 `QWEN_API_KEY`、DashScope 模型权限、可外联网络、Edge、Playwright 以及上述 EgoLife 视频。输出目录必须是新目录，避免覆盖历史证据。
