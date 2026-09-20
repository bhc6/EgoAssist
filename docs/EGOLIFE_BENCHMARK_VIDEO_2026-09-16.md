# EgoLifeQA 原题录屏与 Ground truth 对照

[播放两段视频](../outputs/egolife-benchmark-videos-20260916/index.html) · [链路核验](../outputs/egolife-benchmark-videos-20260916/benchmark-evidence-check.json) · [播放核验](../outputs/egolife-benchmark-videos-20260916/verification.json)

本次直接使用官方 JAKE 的两道题，保留官方中文题干、英文题干、四个选项、标准字母和解释。每段录屏原速展示一分钟视频输入、原题提交、实际回答、预测落盘、标准答案揭晓和比较。每题真实输入 119 个不同视频帧，共 238 帧；问题通过文字输入，合成语音只做朗读，助手声音取自该轮真实模型输出。原视频环境音关闭。

| 官方题号 | 题目 | 实际选择 | Ground truth | 对比 |
| --- | --- | --- | --- | --- |
| 81 | 中午有谁来参观我的房间 | B：Katrina, Shure, Alice | D：Katrina, Lucia, Shure | 不一致 |
| 102 | 早上我们在一楼桌子边讨论聊天时，三角支架是被什么东西给代替了 | 无法确定 | B：小马座椅 | 未选择 |

第一题回答根据外观和选项推断人物姓名，还将未明确出现在画面中的 Alice 纳入答案。这不是可靠的身份依据，原样保留，不按标准答案修改或重试。第二题在输入证据不足时没有选择字母；这不计为答对。

## 输入范围与评测口径

题目来自固定版本 [EgoLifeQA_A1_JAKE.json](https://huggingface.co/datasets/lmms-lab/EgoLife/blob/143fb319be7aa5ae210c936bf4f0f3a86092afb0/EgoLifeQA/EgoLifeQA_A1_JAKE.json)，本地原始文件 SHA256 为 `688ae079f458132f13150711b7e099fbe4fdedd97f19f5a33bdebb7bfde74a52`。

在运行前选择所有至少一个目标时刻落在本地连续视频 DAY1 11:10–11:14:30 内的题目，共两题。Q81 输入 11:13:00–11:14:00；Q102 输入 11:11:00–11:12:00。选片使用了官方目标时刻，属于目标时间辅助的片段演示，不是自动检索到证据的成绩。Q102 另一个目标时刻 11:16:52 不在本地视频里，明确标注缺失。

没有播放截至晚上提问时刻的全天历史，也没有运行完整历史检索。题目允许回答“无法确定”，与官方强制四选一协议不同；这两段不用于推断完整 benchmark 准确率。官方姓名身份表未输入。官方中文与英文解释存在表述差异时均按原文展示，未自行修正。

标准答案、解释和目标时刻不进入模型提问文本。后台演示进程根据目标时刻决定视频窗口；它在预测 JSON 落盘后才读取该轮评分字段并将其显示到页面。各题使用独立会话；预测文件 SHA256 在揭晓后重新验证。评分仅匹配明确的“选择：A/B/C/D”，未选、无法解析计为非匹配，不使用 LLM Judge。

本轮 `scene.turn.frame` 为空，因为提问前已暂停视频并朗读题目；实际回答使用 `scene.turn.observationContext` 中之前形成的模型场景观察。该观察的图像哈希均对应本轮真实发送的帧，核验文件记录 `visualAnswerPath=previous-model-observation`、观察来源 ID、可见证据原文及帧时刻。这说明链路可追溯，不代表该观察完整覆盖一分钟视频或视觉语义完全正确，也不能声称回答模型直接读取了全部 119 帧。

## 运行与修复

原始记录在 `outputs/egolife-benchmark-demo-20260916-r1` 和 `outputs/egolife-benchmark-demo-20260916-r2`。r1 的 Q81 完整完成；切换 Q102 时，录制脚本仍等待已经授权后消失的 `ambientGrant` 按钮，提交题目前超时，因此 r1 总报告仍是 `passed=false`。

脚本已改为按钮存在时点击，支持按 `--cases` 补录。r2 使用新账户仅录制尚未提交的 Q102；没有重跑 Q81，也没有为了答对重试 Q102。两个成功预测都保留，失败记录不删除。没有修改模型提示词、产品逻辑或 benchmark 标准答案来提高本次得分。

导出器接受多个源目录，要求两道预选题各恰好一个完整预测；逐题验证原题一致性、实际输入、画面来源、语音与文字一致、预测未变以及标准答案评分。录屏为 H.264/AAC 1080P，语音依据首包日志近似对齐。浏览器原录屏无音轨，非物理麦克风实测。

## 复现

```powershell
.\.venv\Scripts\python.exe scripts/run_egolife_benchmark_demo.py --output outputs/egolife-benchmark-new --username benchmark-new
.\.venv\Scripts\python.exe scripts/build_egolife_benchmark_videos.py --source outputs/egolife-benchmark-demo-20260916-r1 --source outputs/egolife-benchmark-demo-20260916-r2
.\.venv\Scripts\python.exe scripts/verify_story_videos.py --output outputs/egolife-benchmark-videos-20260916
```

运行默认连接本地端口 8775 的真实 EgoAssist 服务。最终导出包含第一次运行保留的完整 Q81 和补录 Q102。导出文件不含认证状态或浏览器 trace；原始本地认证文件不应打包分享。
