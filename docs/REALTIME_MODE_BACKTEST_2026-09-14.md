# Omni 与模块化模式回测：2026-09-14

## 结论

这次固定案例回测中，两种模式均完成证据问答、中英文转写、简单视觉检查、取消与恢复。Omni 的首段文字、首段音频和视觉返回更快；模块化更早确认取消，也更早发出语音打断的停播事件。模块化未显示回答正确率优势，不能据此宣称更省钱。

保留 Omni 默认模式，模块化作为可选方案。后者的价值是可以分别替换、审计 ASR、视觉、文本和 TTS 模型，而非当前配置已经全面优于 Omni。

任务生命周期在两种模式下均复现了共享语义路由问题，不能把接口检查全通过描述成整个产品全部验收通过。

## 实验口径

- 使用真实 DashScope 调用，无模拟模型。Omni 为 `qwen3.5-omni-flash-realtime`，音色 Ethan；模块化为 `qwen3-asr-flash`、`qwen3-vl-flash`、`qwen-flash`、`qwen3-tts-flash-realtime`，音色 Cherry。
- 固定 seed 为 17、43、91，只控制案例顺序，不控制供应商采样。两种后端交替先后执行，真实对照期间未并行运行其他回测模型请求。
- 每个 seed、每种模式包含六个隔离问答会话，以及一个含两段语音、两张图片、一次取消和恢复的媒体会话。每种模式共 21 个会话，其中问答 18 次、ASR 6 次、视觉 6 次、取消恢复 3 次。
- 六类问答为位置、已更正位置、历史时刻、不同主体、证据不足、来源文本中的恶意指令。每个会话独立上下文，证据、问题和规则在运行前写入 `plan.json`。评测规则不提交给模型；输入证据本身是问答材料。
- 两段中英文合成语音在有效回测中只生成一次，每对后端接收完全相同的 PCM，哈希保存在 `fixtures.json`。简单图像为红色方块和蓝色圆形。独立的应用语音控制复用此前三个 WAV，另行核对 PCM 哈希。
- 回答判分使用预先声明的关键词、排除词和拒答规则；没有使用 LLM as Judge，也没有人工听评音色或逐句语义质量评级。规则通过率不等于通用回答证据支持度。
- 这是小规模、已知案例的功能回归，不是冻结独立测试集，也不是 EgoLife 视频检索回测。没有新增真人或真实视频，因此不报告 Recall、MRR、nDCG、按视频效果或跨参与者泛化；这里的时间/主体问答不证明 Timeline 硬过滤通过。

## 接口层结果

| 指标 | Omni | 模块化 |
| --- | --- | --- |
| 会话通过 | 21/21 | 21/21 |
| 证据问答规则通过 | 18/18 | 18/18 |
| 有实质回答的覆盖率 | 15/18，83.3% | 15/18，83.3% |
| 证据不足时正确拒答 | 3/3 | 3/3 |
| 已回答问题的证据规则通过 | 15/15 | 15/15 |
| 语音规范化后完全匹配 | 6/6 | 6/6 |
| 简单图形与颜色通过 | 6/6 | 6/6 |
| 取消后无迟到音频并恢复回答 | 3/3 | 3/3 |
| 首段文本中位数 | 203 ms | 429 ms |
| 首段音频中位数 | 488 ms | 709 ms |
| 首段音频样本 p95 | 592 ms | 1226 ms |
| 生成完成中位数 | 640 ms | 1061 ms |
| 视觉返回中位数 | 471 ms | 894 ms |
| 语音发送起点至转写中位数 | 3916 ms | 4212 ms |
| 取消确认中位数 | 71 ms | 28 ms |

覆盖率为 83.3% 的原因是三个证据不足样例被正确拒答，不是请求失败。支持率只检查这些简单题目的预定义答案条件；不能与此前真实检索实验的回答支持度直接比较。

首段音频的 18 对配对差值中位数为 **模块化慢 188 ms**。这不同于两个独立中位数相减的 221 ms。每个 seed 两种模式均为 7/7；seed 不构成三个独立数据集。

首段输出从桥接层请求回答开始计时，排除应用语义路由、ASR 和扬声器播放。生成完成指供应商生成结束，不是用户听完。两种音色和实际生成长度不同，会影响完成时间；没有做同音色或音质等价性校准。

语音起点至转写包含约 2.7 至 3 秒语音和端点检测，不是纯 ASR 请求耗时。事件时间在回调发生时记录；音频发送结束至转写可能为负数，因为转写可能在尾部静音发送完之前返回。样本 p95 使用 nearest-rank；18 个问答下它就是最大值，不能作为稳定尾延迟承诺。

## 完整应用语音控制

三条指令分别验证停播但保留答案、暂时安静、取消答案。两种模式的六条直接语义检查和三条真实语音控制均通过。每条指令接收完全相同的输入音频。

| 指令 | Omni 停播事件 | 模块化停播事件 | Omni 语义控制完成 | 模块化语义控制完成 |
| --- | --- | --- | --- | --- |
| 闭嘴。 | 322 ms | 112 ms | 2927 ms | 2857 ms |
| 让我安静一会儿，先别出声。 | 293 ms | 119 ms | 4800 ms | 5120 ms |
| 这个答案我不需要了，不用继续回答。 | 297 ms | 106 ms | 6465 ms | 6585 ms |

停播事件中位数为 Omni 297 ms、模块化 112 ms。语义控制耗时包括输入时长、ASR 和共享路由；中位数分别为 4800 ms 和 5120 ms。因此“更早停播”不代表“更早理解整条指令”。这些测量来自应用 WebSocket，不是物理麦克风、回声消除或扬声器停播测量，也未运行浏览器本地 VAD。

## 任务流程失败

两种模式均成功激活做菜任务，并在用户明确确认后从 `prepare` 推进到 `wash`。第三条“暂停当前任务。”未通过：共享 `qwen3-max` 路由输出 `taskControl=null` 和 `interaction.action=pause`、`output=silent`，实际任务 `paused=false`。验收脚本等待口头回答超时，同时保存了实际任务状态，确认不仅仅是没有播报。

这是两种后端共有的语义路由问题，不是“换回 Omni 就恢复正常”的证据。该顺序验收在此停止，后续重启、恢复任务及退出 Skill 在本轮没有被执行，记为未覆盖。没有修改生产路由或用重试成功掩盖该失败。

## 调用、缓存与成本

模块化接口层共记录 60 次模型请求：ASR 6、VL 6、文本 24、TTS 24，包含被取消的请求。生成两段输入语音另有 2 次 TTS 请求，不计入这 60 次；完整应用的路由等调用也不在其中。常规有声问答需要文本和 TTS 两个请求。

Omni 使用持续实时连接，本脚本未建立与模块化 HTTP/TTS 请求等价的调用口径，因此调用量为 null，而不是 0 或“每题只调用一次”。模块化应用推理结果缓存命中为 0；供应商内部缓存统计以原始 usage 为准，未统一汇总。播放缓存属于另一层，不并入推理缓存。

两种模式费用均为 **null**。token、音频时长和模型定价口径不同，未核对实际账单，不推断节省比例。

## 产物与复现

- [汇总报告](../artifacts/realtime-paired-backtest-20260914-r2/full-report.json)
- [接口层汇总](../artifacts/realtime-paired-backtest-20260914-r2/summary.json)、[逐例结果](../artifacts/realtime-paired-backtest-20260914-r2/results.json)、[运行前协议](../artifacts/realtime-paired-backtest-20260914-r2/plan.json)
- [Omni 语音控制](../artifacts/realtime-backtest-voice-omni-20260914/acceptance.json)、[模块化语音控制](../artifacts/realtime-backtest-voice-modular-20260914/acceptance.json)
- [Omni 任务流程](../artifacts/realtime-backtest-lifecycle-omni-20260914/acceptance.json)、[模块化任务流程](../artifacts/realtime-backtest-lifecycle-modular-20260914/acceptance.json)

首轮 `realtime-paired-backtest-20260914-r1` 因新增脚本的审计回调参数不匹配而无效，未完成有效模型回答，已单独标注并排除。修正回调后运行 r2，音频重新生成，在 r2 内配对复用；r1/r2 不合并统计。生产后端代码本轮没有调整。

```powershell
.venv\Scripts\python.exe scripts/backtest_realtime_modes.py --output artifacts/new-paired-backtest
$env:EGOASSIST_REALTIME_BACKEND='omni'
.venv\Scripts\python.exe scripts/verify_real_voice_stop.py --output artifacts/new-omni-voice --fixture-dir artifacts/modular-voice-stop-20260914-r1
$env:EGOASSIST_REALTIME_BACKEND='modular'
.venv\Scripts\python.exe scripts/verify_real_voice_stop.py --output artifacts/new-modular-voice --fixture-dir artifacts/modular-voice-stop-20260914-r1
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_realtime_backtest_metrics.py tests/test_modular_realtime.py
```

评测规则及后端测试共 21 项通过，`git diff --check` 通过。输入 PCM、逐事件日志和输出 WAV 保存在产物目录；本地文件不保证随仓库克隆分发。
