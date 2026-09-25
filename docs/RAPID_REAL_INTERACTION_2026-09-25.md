# 快速真实交互验证（2026-09-25）

> 后续传输优化及相同模型的交替对照见 [交互延迟优化](LATENCY_OPTIMIZATION_2026-09-25.md)。本文保留优化前的冻结结果。

本轮实际调用 JEV、回答与音频服务，使用隔离账户和数据目录。测试期间修正了答案目标协议和输出渠道提示词，当前提示词为 `decisions-router-v8`。没有重启共享 8770 服务，没有把验收输入写入现有用户会话。

## 最终密集输入

[三条连续输入 r3](../outputs/rapid-live-burst-20260925-r3/report.json) 为当前 v8：每组新建会话，连续发送三个独立计算问题，分别要求不等待、间隔 200 ms、间隔 500 ms。实际间隔、原始事件、每个 turnId 的路由与持久化答案均保留。冻结哈希核对一致。

| 发送间隔 | 正确持久化答案 | 输入回执 | 最新问题首个音频包 | 每题答案完成并被轮询观察到 |
| --- | --- | --- | --- | --- |
| 不等待 | 3/3 | 44.4–46.3 ms | 2549.5 ms | 2740.6 ms |
| 200 ms | 3/3 | 35.7–55.4 ms | 2122.2 ms | 2284.5–2702.1 ms |
| 500 ms | 3/3 | 33.5–40.4 ms | 1981.8 ms | 2358.1–2792.8 ms |

9 个路由均仅用 JEV，实测 642.6–1148.8 ms；回答仍由 `qwen-flash` 生成。旧问题转后台文字完成，最新问题有声音输出，断开 WebSocket 后读取会话仍保留全部答案。不能把旧问题没有音频误报为丢题，也不能把路由延迟当作用户收到答案的总延迟。

验收前预设：回执 ≤1000 ms，最新问题首音 ≤8000 ms，每个持久化答案 ≤30000 ms，并检查答案、真实供应商记录、路由成功和断线后保留。单次开发样本不是吞吐量、p95 或线上 SLA。

## 长会话和真实音频

[长会话 r1](../outputs/rapid-live-long-20260925-r1/report.json) 在修改前的 v6 运行：加载 9193 行历史副本，禁止执行旧待办，使用同一冻结源文件。结果为：

- 两轮顺序文字回执 277.2 / 242.3 ms，首音 4589.4 / 3754.9 ms，分别正确回答 5 和 20。
- 合成语音输入经真实 ASR，输入结束至首音 4537.2 ms，正确回答 56。
- 两条不等待的文字输入都正确，回执 557.4 / 557.5 ms，约 7598.4 ms 内观察到两条持久化答案。
- 带 cProfile 的启动就绪 6130.0 ms，不能视为正常冷启动 SLA。

[浏览器语音 r2](../outputs/rapid-live-voice-browser-20260925-r2/acceptance.json) 在当前 v8 运行，通过真实 Silero VAD、供应商 ASR、JEV 和浏览器播放链路：

- 本地检测开始后停播：从合成音频开始计 159.6 ms；停播时浏览器播放节点为 0。
- 云端 speech-started 停播事件：约 348.7 ms；语义控制落地约 4561.5 ms，二者起点为注入开始。音频本身长 2080 ms，不能把语义控制耗时说成开始停播的耗时。
- 恢复同一 responseId，并保留 32624 samples 的播放游标；新问题可正常播报，静默状态刷新后仍在。
- 1440 / 390 两种宽度通过，无页面 JavaScript 异常。

上述语音是实际 TTS 生成的 PCM，实时送入服务与浏览器；未测物理麦克风、扬声器、回声和真实人群。会话日志显示输入为 Omni server VAD，前台回答实际为 `qwen-flash` 加实时 TTS；不能仅凭脚本顶层模型参数认定整条链路都用同一个模型。

## 暴露的问题与修正

1. [控制 r1](../outputs/rapid-live-controls-20260925-r1/acceptance.json) 续播失败。JEV 选了 resume，但 target=none，系统安全地保持静默。目标题提到 `answerTargets`，实际 state 却没有这个字段；而且上一条纯控制指令可能被加入答案候选。现在发送明确的别名、原问题、状态、是否上一答案/正在播放，并仅从实际 answers/previousAnswer 建候选。目标题独立阅读原话，不依赖同次请求中另一题尚未产生的 route。未知对象仍不猜测。
2. [控制 r2](../outputs/rapid-live-controls-20260925-r2/acceptance.json) 首题没有声音。“用阿拉伯数字计算”被分到 text（0.42，reply 0.40），测试等待音频超时。v8 明确区分数字/字数等内容格式与禁止朗读的输出权限，没有增加特定语句的代码匹配。
3. [浏览器语音 r1](../outputs/rapid-live-voice-browser-20260925-r1/acceptance.json) 只捕获到说话开始，没有等到结束和识别。脚本只补 1000 ms 静音，小于默认 1200 ms 的服务端结束阈值。修正为至少 2000 ms 且超过配置阈值 1000 ms，本次补 2200 ms，并在失败时也保存播放事件。r2 使用新合成语音，因此不是相同音频的严格对照。
4. [密集输入 r1](../outputs/rapid-live-burst-20260925-r1/report.json) 的三个首题输出中文“四十五”，原数字正则误判；输入未要求阿拉伯数字。原分数保留，[独立事后复核](../outputs/rapid-live-burst-20260925-r1/answer-review.json) 为 9/9 正确。后续脚本允许等价中文数字，但仍要求整个短答案正确，不采用宽泛包含匹配。
5. [JEV 控制探测](../outputs/rapid-live-jev-controls-20260925-r1/report.json) 为 8/9；首个请求连接 OpenRouter 失败（ClientConnectorError），未执行错误取消。保留失败，不将其混入后续产品验收成功数。

[控制 r3](../outputs/rapid-live-controls-20260925-r3/acceptance.json) 的文字续答、声音续播、取消旧答案保留新答案、限时静默重连均通过。进一步收紧脚本：文字续答必须明确命中原答案且 output=text，取消必须明确命中原答案；不能仅凭后台答案最终完成掩盖一次 uncertain 控制。

[控制 r4](../outputs/rapid-live-controls-20260925-r4/acceptance.json) 使用上述加强断言复测，4/4 全部通过。未知目标与供应商失败仍保持不执行，本轮没有通过猜测目标或放宽权限换取成功。

## 回归与复现

[全量确定性回归](../outputs/rapid-live-regression-20260925.xml) 在候选修正 v7 后为 843/843，通过耗时 57.858 秒，3 条既有 aiohttp 弃用提示。[最终路由专项](../outputs/rapid-live-router-final-20260925.xml) 在 v8 为 60/60。新增回归检查控制指令不混入答案候选、取消答案不暴露、已完成答案仍可通过别名恢复。

```powershell
.\.venv\Scripts\python.exe scripts/verify_rapid_interaction.py --output outputs/rapid-new
.\.venv\Scripts\python.exe scripts/verify_real_interaction.py --output outputs/controls-new
.\.venv\Scripts\python.exe scripts/verify_real_voice_stop_browser.py --local-vad --output outputs/voice-browser-new
.\.venv\Scripts\python.exe scripts/verify_long_session_latency.py --source outputs/live-latency-fix-20260924-r1/source.jsonl --output outputs/long-new --voice --burst
```

`outputs/` 不提交 Git。历史源会话是本机私有数据；文档仅公布指标，不发布其中原文。已有复杂时间记忆失败仍见 [JEV 优先路由](JEV_FIRST_ROUTER_2026-09-25.md)，本轮快速交互通过不代表该问题已修复。
