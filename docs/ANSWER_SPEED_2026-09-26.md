# 回答速度：语音结束等待对照

日期：2026-09-26。**1000 ms 已正式启用到本地 8770 服务。** 充值后完成真实完整链路对照，首音中位数从 2.75 秒降至 2.42 秒，12/12 正确回答。

## 实际链路

正式服务会话 `2fc75e811dd94807` 的 `provider.session.ready` 确认语音输入使用 Omni 服务端 VAD。文字输入的回答走 qwen-flash + 流式 TTS，因此不能仅凭 `modular-` 回答 ID 判断麦克风后端。Omni 的结束等待由 `EGOASSIST_SPEECH_END_SILENCE_MS` 控制，原默认 1200 ms；可选模块化后端的 WebRTC VAD 使用另一套 660 ms 结束条件。

已有 TTS 预取能把建连与文字生成重叠。上述正式会话的两个 TTS 样本中，session ready 约 101/104 ms，文字首片段约 303/494 ms，提前建立 TTS 连接没有显示出明显收益。本轮将 Omni 默认结束等待从 1200 ms 调整为 1000 ms，JEV 判断和记忆流程保持原实现。显式环境配置仍优先于默认值，可设置 1200 恢复原等待；模块化后端的 660 ms 条件不受该参数影响。

## 充值后：完整回答对照

复用上轮三份合成音频，短问题及带插入停顿的组合问句各重复三次，交替 1200/1000 ms 顺序。每次新建隔离会话；使用真实 Omni 语音输入和语音输出，以及 JEV v10 提示词。实验期间没有并行运行浏览器验收或测试套件。

| 指标 | 1200 ms | 1000 ms |
| --- | ---: | ---: |
| 完整识别且正确完成语音回答 | 6/6 | 6/6 |
| PCM 输入结束至转写中位数 | 1398.2 ms | 1144.9 ms |
| PCM 输入结束至首音中位数 | 2751.9 ms | 2418.1 ms |

本轮首音中位提前 333.8 ms，约 **12.1%**。全部 12 次 JEV 分类成功，无额度错误，组合问句均保持单段完整转写。输入 PCM、原始结果和源码/提示词哈希见[完整对照](../outputs/answer-speed-20260926/credited-ab-1000/report.json)。

收益不是每次保证：最后一个 1000 ms 样本的 JEV 耗时 1373.1 ms，首音 3278.7 ms，慢于同组 1200 ms 的 2590.1 ms。结束阈值只减少 200 ms，其余差异包含网络、识别及生成波动。这里只有两个开发输入、各阈值六个样本；不作为 p95、长会话或物理麦克风的性能结论，也没有证明文字输入提速。

候选值的[真实 Edge 浏览器验收](../outputs/answer-speed-20260926/credited-browser-1000/acceptance.json)七项全部通过：首问发音、附和及本地 Silero 不停播、EgoLife 回放不抢话、语音换题、暂等后新问题、快速输入后附和保留原回答、明确停止清空设备队列。无页面错误，1440/390 宽度无横向溢出。测试为合成语音及已有 EgoLife 录音输入，不是物理麦克风或回声消除测量。

## 正式启用与回归

- [相关回归](../outputs/answer-speed-20260926/credited-regression.log)：107/107，36.87 秒。覆盖默认值和环境覆盖、语义发言权、连续输入、实时 API、模块化生命周期、文字及延迟回归。这次只调整端点默认值，未重复执行全量套件。
- 启动时原 8770 无监听且没有原服务进程；核对数据根目录锁可用后，以原入口和 `.egoassist` 启动，健康检查 200。[启动记录](../outputs/answer-speed-20260926/release-1000/startup.json)。
- [正式服务浏览器验收](../outputs/answer-speed-20260926/credited-production-1000/acceptance.json)：隔离测试账号的同样七项检查全部通过，页面无异常。会话 `2d53fdc0168c4cb6` 的 `model.prompt/session.update` 明确记录 `speechEndSilenceMs=1000`，`provider.session.ready` 确认 Omni 服务端 VAD，JEV 分类成功。

默认值与 `.env.example` 同步更新。已有环境若显式设置 `EGOASSIST_SPEECH_END_SILENCE_MS=1200`，需要调整或移除覆盖后重启；本地 `.env` 没有此覆盖，已经使用新默认值。保留 800 ms 失败及先前额度中断结果，不把正式验收替代交替性能对照。

## 800 ms：未采用

同一批合成 PCM，短计算问题及“助手，请帮我算一下”与后半问句之间插入 500 ms 静音的组合，各做三轮 1200/800 ms 交替对照。插入静音之外还存在 TTS 自身的首尾静音，不能将其称为精确 500 ms 自然停顿。

- 初始短问题：转写从约 1414 ms 提前到 1012 ms；首音从约 3467 ms 到 2486 ms。单个样本包含网络与供应商波动，不可外推。
- 800 ms 将组合问句拆开，前两轮没有完成语音回答。1200 ms 在相应两轮保留整句并正确回答。提前断句和未回应同时出现，不足以单独证明全部失败原因。
- 后续一个 1200 ms 样本出现 Omni `response.create` 超时；最后三个样本的 JEV 请求返回 HTTP 402，供应商原文为 `Insufficient credits`，`limit_source=openrouter_credits`。这些失败全部保留，不计算完整实验的提速百分比。
- 独立真实 Edge 浏览器验收的七项检查通过，包括附和不停播、EgoLife 多人会议片段不抢话、换题、暂等后提问、快速输入及语义停止。浏览器验收与部分初期对照重叠运行，初期首音也不作为最终性能估计。

证据：[初期完整对照](../outputs/answer-speed-20260926/ab/report.json)、[800 ms 浏览器验收](../outputs/answer-speed-20260926/browser-800/acceptance.json)。EgoLife 回放没有说话人及对助手说话的人工真值；其结果仅为本场景开发检查。

## 先前受阻阶段：仅识别验证

JEV 额度不足后停止完整链路实验，改用同一份已合成音频直接检查真实 Omni 识别，不调用 JEV 推理，也不产生回答。两种输入、两轮交替，每个阈值四个样本。复核所有转写均保留完整计算问题。

| 指标 | 1200 ms | 1000 ms |
| --- | ---: | ---: |
| 单段完整转写 | 4/4 | 4/4 |
| PCM 输入结束至转写中位数 | 1327.8 ms | 1089.4 ms |
| PCM 输入结束至首音 | 未测量 | 未测量 |

观察到转写提前 238.4 ms，约 18%。阈值本身减少 200 ms，其余差异包含网络和供应商波动；不是物理麦克风测量、p95 或完整回答延迟。这个阶段只有两个开发输入，当时未把 1000 ms 写入正式默认值或重启服务。充值后的完整回答结果另列于上方。

证据：[独立识别对照](../outputs/answer-speed-20260926/asr-1000/report.json)。测试没有修改用户数据，使用隔离目录；`outputs/` 不提交 Git。

## 复测入口

新增 `scripts/compare_speech_endpoint.py`，使用同一份合成音频交替参数，记录每次失败、识别、首音、回答、路由状态和后端。完整模式遇到账户权限或额度错误即保留报告并停止剩余试验；`--asr-only` 明确不测回答延迟。

以下命令可在新目录复现完整对照及日常浏览器验收；显式设定 1000 ms 可验证旧版默认值仍为 1200 ms 的检出版本。

```powershell
.\.venv\Scripts\python.exe scripts/compare_speech_endpoint.py --output outputs/answer-speed-next --candidate-ms 1000 --repeats 3 --fixture-dir outputs/answer-speed-20260926/ab
$env:EGOASSIST_SPEECH_END_SILENCE_MS='1000'
.\.venv\Scripts\python.exe scripts/verify_daily_dialogue_browser.py --output outputs/answer-speed-next-browser
Remove-Item Env:EGOASSIST_SPEECH_END_SILENCE_MS
```

没有本地音频的环境可省略 `--fixture-dir`，首次通过 Qwen TTS 合成，随后同轮所有参数复用同一 PCM。完整测试需要可用的 Qwen 和 OpenRouter 额度。
