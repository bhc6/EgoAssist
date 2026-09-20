# 生活化多轮对话演示 · 2026-09-16

[四段视频播放页](../outputs/natural-story-videos-20260916/index.html) · [打包下载](../outputs/natural-story-videos-20260916/natural-dialogue-videos.zip) · [视频与链路索引](../outputs/natural-story-videos-20260916/manifest.json) · [播放核验](../outputs/natural-story-videos-20260916/verification.json)

根据“自然语言、生活化长对话、多轮来回”的要求，重新通过真实浏览器、真实路由和真实云端回答模型运行 38 轮输入。四段为完整连续场景，每段保留实际等待、补充、更正和追问，末尾停留 4 秒。此前 20–60 秒的功能短片仍保留。

| 场景 | 轮数 | 时长 | 本轮实际行为 | 原始运行 |
| --- | --- | --- | --- | --- |
| 老人：陪我记住家里的小事 | 10 | 2 分 37 秒 | 区分日常与备用钥匙；更正到黄色抽屉；刷新后回忆；替用户组织告诉老伴的话 | `natural-story-20260916-r4-elder` |
| 职业：把今天的忙碌说成经历 | 10 | 3 分 48 秒 | 记录真实视频工作；整理、缩短周报；明确真实用户体验尚未开展；形成项目介绍 | `natural-story-20260916-r4-career-life` 的 career 案例 |
| 生活：出门前陪我检查 | 10 | 2 分 44 秒 | 两件找到不代表三件找到；桌上不等于装包；自然暂停、刷新恢复；提醒遗漏的门禁卡 | `natural-story-20260916-r5-life` |
| 无障碍：慢慢说，跟着听 | 8 | 2 分 54 秒 | 对同一真实图片连续追问；描述颜色和物品；小字看不清时说明；第 7 轮通过真实 ASR 输入 | `natural-story-20260916-r2` 的 access 案例 |

例如生活场景的真实对话：

> 演示者：我把钥匙放进包里了，手机拿在手上，这样算都带好了吗？
>
> EgoAssist：好的，钥匙在包里，手机在手上了。那门禁卡呢？你确认它也在包或者口袋里了吗？
>
> 演示者：哦对，门禁卡还在桌上。你刚才要是不提醒，我真就走了。
>
> EgoAssist：别急，门禁卡还在桌上对吧？那你现在把它放进包里或者口袋里了吗？确认一下再走。

## 音画来源

- 演示者台词由 Qwen TTS 生成，以区别于没有声音的文字输入。通常通过控制台文字输入；无障碍第 7 轮实际发送 PCM，经过实时 ASR。不能将其称为真人麦克风实测。
- 助手声音来自该轮实际 responseId 的原始音频，不预写或重生成模型回答。保存操作只有文字回执时，另用 TTS 原文朗读，并在视频标注“保存回执 · 原文合成朗读”。
- 浏览器原录屏没有系统音轨。合成的演示者声音和实际回答声音按日志墙钟近似对齐，不能据此宣称毫秒级实机端到端延迟。保留了实时等待和原速操作，右侧对话文字为后期加注。
- 每个 turnId、音频类型、响应 ID、源录屏区间、文件哈希和原始报告均写入 manifest。图片为该轮实际输入的 EgoLife 帧；画面边栏中的缩略图明确标注为后期展示。
- 这些是实际系统运行的演示情境，尚非老人或残障人士实地研究。生活任务的完成来自演示者陈述，不是摄像头证实实际装包。

## 长对话发现的问题与修复

1. **普通补充导致整轮静默。** 记忆模型偶尔把 observed consent 和 participant scope 混用，旧校验在可选记忆组件之外抛错，导致整轮被视为不确定。现在在组件内拒绝该记忆提案，保留有效对话与任务控制；并补充当前轮授权边界。未通过的记忆不会因此获得持久权限。
2. **口语“出门检查”没有选到步骤任务。** 将它纳入通勤检查任务的正式别名，保留旅行打包的区分，不设置通用默认任务。
3. **部分完成被当成整个步骤完成。** 为路由补齐当前步骤、要求和完成条件；仅靠主路由仍有误判，因此对确认提案增加独立、受约束的真实模型复核。未满足全部条件或复核失败时不推进步骤。请求、结果与耗时保留在 `taskControlCheck` 中。
4. **离开开门被当成暂停旧答案。** 对未完成任务中的 pause/resume 提案区分任务与回答；经复核确认是任务时，仅操作任务，保留正常口头回应。r5 成功保留暂停状态并刷新恢复。
5. **工作复盘遗漏“还没做”。** 加强事实陈述、物品状态、未完成工作和后续追问的约束；最终周报与简短版本明确保留老人及残障人士真实体验未开展。

失败未删除：r1 记录了静默、早完成和遗漏事实；其早期 `passed` 只表示输入终态与日志检查，不能视作场景语义通过。后续脚本增加了场景语义和任务状态检查。r2 和 r4-career-life 的 life 案例明确失败；r2 career 与 r3-elder 还记录了回执配音 TTS 超时。最终视频只使用上表指定的完整通过案例，不混接一个场景内的不同运行。

`natural-task-review-probe.json` 另记录真实模型对“部分找到”“桌上未装包”“离开开门”“返回继续”的复核结果。最终生活场景验证了三步状态、暂停恢复、部分携带不完成和全部确认后完成。

完整回归测试 640 项通过；有 3 条既有 aiohttp 超时参数弃用警告。新回归覆盖记忆组件失败隔离、任务别名边界、当前完成条件传递、部分确认拒绝和任务暂停与回答暂停区分。

## 复现

先启动项目，实际模型配置沿用本机 `.env`；脚本不会写入预设助手答案。

```powershell
.\.venv\Scripts\python.exe run_egoassist.py --port 8771 --memory-path .egoassist-natural-demo/runtime.json
.\.venv\Scripts\python.exe scripts/run_natural_story_showcase.py --output outputs/natural-story-new --username natural-story-new
```

从本次保留证据构建视频并检查播放：

```powershell
.\.venv\Scripts\python.exe scripts/build_natural_story_videos.py --source outputs/natural-story-20260916-r2 --source outputs/natural-story-20260916-r4-career-life --source outputs/natural-story-20260916-r4-elder --source outputs/natural-story-20260916-r5-life
.\.venv\Scripts\python.exe scripts/verify_story_videos.py --output outputs/natural-story-videos-20260916
```

输出格式为 1920×1080、H.264/AAC。核验包含全帧解码、非静音音轨、Edge 实际播放、1440/390 宽度布局；不需要在线模型即可播放导出文件。
