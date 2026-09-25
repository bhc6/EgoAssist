# 回答速度：语音结束等待对照

日期：2026-09-26。**状态：1000 ms 为候选，尚未正式启用。生产默认仍为 1200 ms。**

## 实际链路

正式服务会话 `2fc75e811dd94807` 的 `provider.session.ready` 确认语音输入使用 Omni 服务端 VAD。文字输入的回答走 qwen-flash + 流式 TTS，因此不能仅凭 `modular-` 回答 ID 判断麦克风后端。Omni 的结束等待由 `EGOASSIST_SPEECH_END_SILENCE_MS` 控制，原默认 1200 ms；可选模块化后端的 WebRTC VAD 使用另一套 660 ms 结束条件。

已有 TTS 预取能把建连与文字生成重叠。上述正式会话的两个 TTS 样本中，session ready 约 101/104 ms，文字首片段约 303/494 ms，提前建立 TTS 连接没有显示出明显收益。本轮针对实际 Omni 结束等待进行实验，没有修改 JEV 判断、记忆流程或正式运行参数。

## 800 ms：未采用

同一批合成 PCM，短计算问题及“助手，请帮我算一下”与后半问句之间插入 500 ms 静音的组合，各做三轮 1200/800 ms 交替对照。插入静音之外还存在 TTS 自身的首尾静音，不能将其称为精确 500 ms 自然停顿。

- 初始短问题：转写从约 1414 ms 提前到 1012 ms；首音从约 3467 ms 到 2486 ms。单个样本包含网络与供应商波动，不可外推。
- 800 ms 将组合问句拆开，前两轮没有完成语音回答。1200 ms 在相应两轮保留整句并正确回答。提前断句和未回应同时出现，不足以单独证明全部失败原因。
- 后续一个 1200 ms 样本出现 Omni `response.create` 超时；最后三个样本的 JEV 请求返回 HTTP 402，供应商原文为 `Insufficient credits`，`limit_source=openrouter_credits`。这些失败全部保留，不计算完整实验的提速百分比。
- 独立真实 Edge 浏览器验收的七项检查通过，包括附和不停播、EgoLife 多人会议片段不抢话、换题、暂等后提问、快速输入及语义停止。浏览器验收与部分初期对照重叠运行，初期首音也不作为最终性能估计。

证据：[初期完整对照](../outputs/answer-speed-20260926/ab/report.json)、[800 ms 浏览器验收](../outputs/answer-speed-20260926/browser-800/acceptance.json)。EgoLife 回放没有说话人及对助手说话的人工真值；其结果仅为本场景开发检查。

## 1000 ms：仅识别验证完成

JEV 额度不足后停止完整链路实验，改用同一份已合成音频直接检查真实 Omni 识别，不调用 JEV 推理，也不产生回答。两种输入、两轮交替，每个阈值四个样本。复核所有转写均保留完整计算问题。

| 指标 | 1200 ms | 1000 ms |
| --- | ---: | ---: |
| 单段完整转写 | 4/4 | 4/4 |
| PCM 输入结束至转写中位数 | 1327.8 ms | 1089.4 ms |
| PCM 输入结束至首音 | 未测量 | 未测量 |

观察到转写提前 238.4 ms，约 18%。阈值本身减少 200 ms，其余差异包含网络和供应商波动；不是物理麦克风测量、p95 或完整回答延迟。只有两个开发输入，尚未验证更长停顿、不同说话习惯及完整 JEV 控制回归。因此未把 1000 ms 写入正式默认值或重启服务。

证据：[独立识别对照](../outputs/answer-speed-20260926/asr-1000/report.json)。测试没有修改用户数据，使用隔离目录；`outputs/` 不提交 Git。

## 复测入口

新增 `scripts/compare_speech_endpoint.py`，使用同一份合成音频交替参数，记录每次失败、识别、首音、回答、路由状态和后端。完整模式遇到账户权限或额度错误即保留报告并停止剩余试验；`--asr-only` 明确不测回答延迟。

恢复 OpenRouter 额度后，用新目录运行完整候选对照，然后重跑日常浏览器验收。通过后才能决定正式启用；不把识别更快当作已经证明回答更快。

```powershell
.\.venv\Scripts\python.exe scripts/compare_speech_endpoint.py --output outputs/answer-speed-next --candidate-ms 1000 --repeats 3 --fixture-dir outputs/answer-speed-20260926/ab
$env:EGOASSIST_SPEECH_END_SILENCE_MS='1000'
.\.venv\Scripts\python.exe scripts/verify_daily_dialogue_browser.py --output outputs/answer-speed-next-browser
Remove-Item Env:EGOASSIST_SPEECH_END_SILENCE_MS
```

没有本地音频的环境可省略 `--fixture-dir`，首次通过 Qwen TTS 合成，随后同轮所有参数复用同一 PCM。完整测试需要可用的 Qwen 和 OpenRouter 额度。
