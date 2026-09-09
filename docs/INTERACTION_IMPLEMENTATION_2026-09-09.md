# 自然打断实现与真实验收

本轮落实[此前审查](INTERRUPTION_RESEARCH_2026-09-09.md)中的软件改造。结论限于以下真实 Qwen、浏览器和自动测试范围；没有完成眼镜硬件、多人录音、噪声和回声消除验收。项目此前报告的复杂时间检索和月报截断问题不属于本轮验收通过项。

## 执行方式

1. 浏览器使用 Silero v5 检测开口，立即停止已排队音源并上报播放位置；云端 VAD 保留为降级路径。停止声音不等待完整 ASR 或语义判断。
2. Qwen 结合当前完整转写、近期对话、最多 24 个回答候选、播放证据和输出权限，返回对话对象、操作、输出通道、目标 ID 与可选静音期限。没有中文停止关键词匹配，也不使用模型自报置信度授权操作。
3. 确定性校验限制字段、时长、已有目标 ID 和证据原文。拟取消答案时，独立短请求再比较当前话语与原问题；否决或失败会保留答案。该核验也是模型判断，并非正确性证明。
4. 取消仅作用于指定答案，独立提醒等工作不随之取消。文字续答沿用原问题的任务与证据，不把“继续”创建成新问题；新问题仍可让旧回答转为后台文字完成。
5. 免打扰期限和未确认输入造成的输出暂停写入案例日志。重连、刷新或服务重建不会自动放开声音。允许直接回复时，仅授予当前轮次播报权限，主动输出仍受限制。
6. 有实际设备进度及缓存时，从未播放的 PCM 采样点恢复原响应。默认每响应缓存最多 90 秒、最多 8 个响应；内存缓存不跨重启保留。缓存缺失、溢出或没有设备进度时，基于原问题保守重新组织回答，不伪造逐字衔接。

附和、句内口误和指向第三人的话交给语义模型解释。只有本地 VAD 明确报告误触发、云端没有未决输入、输出代次未改变且没有静音限制时，才允许缓存恢复。ASR 空结果不能单独解除停播。

## 模块与审计

| 文件 | 职责 |
| --- | --- |
| `realtime/interaction.py` | 协议校验、持久输出权限、受限 PCM 缓存与设备进度 |
| `realtime/interaction_controller.py` | 应用控制决策，操作原答、取消、文字续答与音频恢复 |
| `realtime/semantic.py` | 语义分类、取消核验和真实请求指标 |
| `product/answer_jobs.py` | 原问题后台补答、暂停和恢复；恢复时重新计算前台截止时间 |
| `console/local_vad.js` | 本地 Silero 检测及降级反馈 |
| `console/playback_audit.js` | 浏览器 AudioContext 采样位置、停播上报与续播偏移 |
| `prompts/interaction.json` | 唯一交互语义提示词、取消核验、恢复与播放证据约束 |

表中路径相对 `egoassist/`。删除了 `answer_completion.json` 中重复的控制提示词。真实请求、取消核验输入/输出、分类重试、播放位置与恢复方式可从案例日志和提示词审计查看。前端区分生成完成与设备报告播放完成，并显示免打扰期限、等待意图、重试和任务状态。

生成的字幕可能从未播放。普通回答及恢复请求都明确携带这一证据边界；当前没有对齐词时间戳，也没有声称已把 Qwen 服务端历史逐字截断。设备播放位置也不能证明佩戴者听到或理解。

## 真实结果

模型：`qwen3-max` 路由与取消核验，`qwen3.5-omni-flash-realtime` ASR/回答，`qwen3-tts-flash` 合成测试语音。音频测试使用真实服务和浏览器 AudioContext；合成输入不是真人录音。

| 验证 | 结果 | 本地证据目录 |
| --- | --- | --- |
| 全量自动回归 | 402 通过，0 失败，0 跳过；自动测试包含模拟依赖 | `outputs/interaction-full-final-2.xml` |
| 问题归属真实链路 | 快速提问、打断、更正、重复重连、在途断线、自动补答、取消共 7 场景通过 | `outputs/interaction-v16-attribution-final/` |
| 应用控制真实链路 | 原问题文字续答、取消指定旧答案且不创建重复任务、静音重连并允许直接回复，3 场景通过 | `outputs/interaction-v16-controls-4/` |
| 真实语音停止 | 静音与撤回答案共 3 段音频通过；另有 6 条语义断言 | `outputs/interaction-v16-voice-acceptance/` |
| 最终真实浏览器 | 本地 VAD、缓存续播、新问题播放、静音刷新通过；1440/390 视口无页面错误或横向溢出，已检查截图 | `outputs/interaction-v16-browser-layout-final/` |
| 语义对照探测 | 20 个编写样例重复两轮，36 次明确断言全部通过，4 次歧义输入不强判 | `outputs/interaction-v16-verified/` |
| 六段真实音频观察 | 观察口误、第三人、委婉静音、撤回、期限和文字输出；作为研究日志，不冒充完整效果验收 | `outputs/interaction-v16-audio-1/` |

语义探测参与了调参，不是独立保留测试集。较快的 `qwen-flash`、`qwen-plus` 在本组控制目标和句内修正上有错误；`qwen3.5-flash` 在使用的文本接口不可用。`qwen3-max` 单独分类仍有错误取消目标，增加核验后才通过上述明确断言。这是当前选型的局部证据，不是全面模型排名。

最终浏览器测量：

| 指标 | 测量值及起点 |
| --- | --- |
| 本地停播 | 187.5 ms，从测试 PCM 播入本地 VAD 的记录时刻到停止音源回调；停止后音源数 0 |
| 设备 VAD 服务端停播通知 | 202.1 ms，从测试注入流程启动计时 |
| 云端 VAD 停播通知 | 337.3 ms，从同一注入流程启动计时 |
| 完整静音决策 | 4253.3 ms，从输入流程启动计时；包含 1920 ms 输入音频、ASR 结束判断和路由 |
| 原响应恢复 | 原 `responseId`，偏移 13872 个采样点；没有从头重播 |

不要把 4.25 秒完整决策时间说成停播延迟，也不要把 187.5 ms 说成耳机声学延迟。另一轮真实浏览器停播为 187.7 ms，早期轮次为 203.9 ms；样本不足以估计稳定的 p95。

最终三个应用场景的路由调用为 936.2–2245.1 ms，指定旧答取消含二次核验为 2245.1 ms。此前语义探测中位数 1159.2 ms、最大 2615.2 ms。普通问题只有一次分类；取消可能再耗一个 4 秒核验窗口，失败分类最多自动重试一次。即时停播与这些请求解耦。性能瓶颈仍主要在 ASR 结束判断和云端语义处理，缓存恢复不需重新生成音频。

## 保留的失败

- `interaction-v16-controls-2`：模型把“保留另一个原答案”输出为新的对话任务，发生重复补答。已更新提示词，最终验收明确断言控制轮不能创建第三个回答任务。
- `interaction-v16-controls-3`：立即重连时旧连接仍持有锁，HTTP 409。浏览器已有有界退避；测试按相同路径执行并记录冲突。最终轮经历一次 409 后成功，不把冲突删去。
- `interaction-v16-browser-final-1`、`controls-1`：执行沙箱拒绝 DashScope 外联，真实服务未完成调用。
- `interaction-v16-browser-final-2`：TTS 请求超时，未进入浏览器测试。后续新目录重跑成功，原失败保留。
- `interaction-full-2.xml`：三个中间回归失败，分别为答案通道兼容、部分控制字段、新增重试事件断言，均已修复。

`outputs/` 被 Git 忽略，完整日志保留在当前工作区；本文随代码提交。外部克隆需按下方命令重新生成原始证据。

## 复现与部署边界

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe scripts/verify_real_interaction.py --output outputs/new-interaction
.venv\Scripts\python.exe scripts/verify_real_voice_stop.py --output outputs/new-voice
.venv\Scripts\python.exe scripts/verify_real_voice_stop_browser.py --local-vad --output outputs/new-browser
.venv\Scripts\python.exe scripts/verify_real_turn_attribution.py --output outputs/new-attribution
```

需要真实 Qwen 凭据、可访问 DashScope 的网络、Playwright 和 Edge。输出目录必须未存在。VAD 所需 ONNX/WASM 与许可证随 wheel 分发，已构建 wheel 并确认 9 个 VAD 资源都在包内。前台设备连接关闭后不能持续采集环境音。

仍需硬件到位后验证真人口误、多人说话、回声、噪声、蓝牙缓存、丢包、电量、功耗，以及真实 LiveKit 服务链路。本轮实测媒体通道是 WebSocket；本地 VAD 故障会显示云端降级。任何有限自然语言测试都不能证明没有误判；核验失败会保留问题并限制播报，而不是声称已经取消成功。
