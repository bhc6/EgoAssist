# 可切换实时后端与验收：2026-09-14

已接入 `modular` 后端：ASR、VL、文本模型和流式 TTS 分别承担识别、观察、回答和播报。2026-09-26 的真实语音对照通过后，`modular` 已成为默认后端；`omni` 保留为显式对照和回退路径。当前证据支持模块化方案能完成已测流程，不宣称它在所有质量、费用或延迟指标上全面优于 Omni。

## 三链路如何使用模型

| 职责 | modular 实现 | 与共享系统的关系 |
| --- | --- | --- |
| 语音输入 | WebRTC VAD 分段，`qwen3-asr-flash` 转写 | 转写进入原有交互调度和语义路由 |
| 观察 | `qwen3-vl-flash` 处理采样图像 | 独立观察连接；问题相关证据继续由原有视觉预取和上下文框架绑定 |
| 回答 | `qwen-flash` 流式生成文本 | 接收原有上下文、检索证据和工具回执 |
| 播报 | `qwen3-tts-flash-realtime`，Cherry 音色 | PCM16、单声道、24 kHz，复用原有播放、打断和续播 |
| 记忆及其他交互模型 | 保留原有检索、路由、工具、补答和报告服务 | 后端切换不替代这些服务，三链路也不等于三个模型 |

主实现为 `egoassist/realtime/modular.py`。它适配 `OmniBrowserBridge` 已有会话接口；桥接类名称保留，但可装配两种传输。权限、任务推进、工具执行、输入顺序、持久上下文与重连仍由共享实现负责。

原始音频交给 ASR，原始采样画面交给 VL。模块化回答模型接收文本及可追溯证据，不直接消费原始声画。检索到的原文片段仍可进入文本上下文；不是把所有原始材料全部放入上下文。

语音输入是 VAD 分段后的批次 ASR，不是逐 token 的实时 ASR。服务端采用 20 ms 帧、300 ms 前置音频、约 660 ms 静音端点，单段默认最多 20 秒；队列和历史上下文有界。输出文本增量送入流式 TTS。取消输出关闭相关文本/TTS 请求，不取消独立 ASR。失败和取消不记为回答完成。

`modular` 的连接就绪表示本地管线已经准备好，实际 HTTP/TTS 请求按需建立；它不表示所有供应商模型都已成功请求。原始音频的语气、非语言声音和声画精细同步能力尚未做替代等价性验证。

## 启动与配置

```powershell
.venv\Scripts\python.exe -m pip install -e ".[modular]"
.venv\Scripts\python.exe run_egoassist.py --realtime-backend modular --open
```

使用现有 `QWEN_API_KEY` 或 `DASHSCOPE_API_KEY`。模型需要对应 DashScope 权限及 HTTPS/WebSocket 网络访问。切回 Omni：

```powershell
.venv\Scripts\python.exe run_egoassist.py --realtime-backend omni --open
```

也可设置 `EGOASSIST_REALTIME_BACKEND`；命令行优先。`--model` 选择 Omni 模型，模块化模型使用下列配置。

| 环境变量 | 默认值 |
| --- | --- |
| `EGOASSIST_MODULAR_TEXT_MODEL` | `qwen-flash` |
| `EGOASSIST_VISUAL_QUESTION_MODEL` | `qwen3-vl-flash` |
| `EGOASSIST_ASR_MODEL` | `qwen3-asr-flash` |
| `EGOASSIST_MODULAR_TTS_MODEL` | `qwen3-tts-flash-realtime` |
| `EGOASSIST_MODULAR_VOICE` | `Cherry` |
| `EGOASSIST_MODULAR_TIMEOUT` | 45 秒 |
| `EGOASSIST_ASR_TIMEOUT` | 10 秒 |
| `EGOASSIST_MODULAR_MAX_AUDIO_SECONDS` | 20 秒 |
| `EGOASSIST_MODULAR_ASR_QUEUE` | 4 段 |
| `EGOASSIST_MODULAR_HISTORY_CHARS` | 48000 字符 |

视觉观察沿用 `EGOASSIST_PERCEPTION_TIMEOUT`。问题视觉预取仍沿用自身超时。当前 TTS WebSocket 使用 DashScope 中国站端点；不宣称任意供应商或地区可直接替换。

## 实测结果

回归命令覆盖新后端、上下文桥接、重连、DNS、实时 API、架构边界、观察、交互和工具：**210 项通过**。结果见 [regression.xml](../artifacts/modular-realtime-20260914-r5/regression.xml)。新测试包括流中断/截断、取消、ASR 失败与队列满、历史限额、独立视觉、原上下文重放和用量转发。

真实桥接对照使用同一段合成语音、同一张红色方块图片和同一组问题；每个后端两轮，运行顺序交替。两种后端均完成桌上蓝杯的证据回答、语音转写、图像识别、长回答取消和取消后的新回答。

| 后端 | 通过轮次 | 首段音频，两轮（ms） | 取消确认，两轮（ms） | 每轮模块请求计数 | 应用推理缓存命中 | 费用 |
| --- | --- | --- | --- | --- | --- | --- |
| modular | 2/2 | 790.486 / 705.738 | 22.492 / 31.301 | 8 | 0 | null |
| Omni | 2/2 | 529.096 / 533.076 | 111.546 / 77.435 | null | null | null |

模块化每轮计数为 ASR 1、VL 1、文本 3、TTS 3，包含被取消的回答请求，不包含生成测试语音的调用。Omni 未做同口径计数，保留 null。应用未启用模型结果缓存；该 0 不代表供应商侧缓存命中为 0，也不代表播放音频缓存未使用。各阶段供应商用量保存在审计中，页面显示回答文本模型用量，不能将其视为整个语音管线的总费用。

首段音频从桥接层请求回答起算，排除 ASR、语义路由和实际扬声器播放。取消确认也不是硬件停播测量。语音起点到转写约 4.5 至 4.9 秒，包含测试语音时长与静音，不是 ASR 请求延迟。原始事件及阶段指标见 [comparison.json](../artifacts/modular-realtime-20260914-r5/comparison.json)。

这组样本中 modular 首段音频更慢。只有两轮简单样例，且两种后端音色不同；不能据此宣称质量、尾延迟、跨参与者泛化或成本优势。

## 浏览器与完整应用验证

- [真实语音控制](../artifacts/modular-voice-stop-20260914-r1/acceptance.json)：停播、暂时安静、取消当前答案三条指令通过；音频发送至应用停播事件约 117 至 120 ms。
- [Edge 浏览器](../artifacts/modular-browser-20260914-r1/acceptance.json)：真实 Silero VAD、停播后零播放源、按原响应与 24944 样本位置续播、新问题播报、静音权限刷新保留通过；1440/390 宽度无水平溢出，无页面异常。
- [最终页面检查](../artifacts/modular-demo-20260914/smoke.json)：最后的用量转发与流清理修改后，真实文本问答、音频及页面用量通过。回答模型输入 1388、输出 7 tokens；该次首段音频为 635.779 ms，独立样例不并入上表对照。

上述音频来自真实 TTS 后通过软件发送到服务/浏览器，未测物理麦克风、摄像头、扬声器、眼镜、回声消除或噪声环境。桌面/移动宽度截图只验证布局。

## 失败与未完成覆盖

早期对照产物 `modular-realtime-20260914-r1` 至 `r4` 保留。r1/r2 修正了测试框架调用与终态等待；r3/r4 记录视觉 JSON 格式失败。r5 对两个后端使用相同的严格 JSON 提示，模块化 VL 同时请求 JSON 响应格式。它是开发过程中的协议修复结果，不是冻结独立测试集。

[Skill 生命周期验收](../artifacts/modular-lifecycle-20260914-r1/acceptance.json) 未通过：任务激活与用户确认推进已通过，但“暂停当前任务。”被共享语义路由输出为 `taskControl=null`、`interaction.action=pause`、`output=silent`，脚本等待任务暂停后的口头回答超时。当前证据不能宣称任务生命周期全部通过；该语义路由问题仍待单独修正与验证。

## 复现

真实调用会消耗 API 配额；每次使用新目录保留历史。

```powershell
.venv\Scripts\python.exe scripts/compare_realtime_backends.py --output artifacts/new-backend-comparison --repetitions 2
$env:EGOASSIST_REALTIME_BACKEND='modular'
.venv\Scripts\python.exe scripts/verify_real_voice_stop.py --output artifacts/new-modular-voice
.venv\Scripts\python.exe scripts/verify_real_voice_stop_browser.py --output artifacts/new-modular-browser --local-vad
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_modular_realtime.py tests/test_context_harness_bridge.py tests/test_omni_dns.py tests/test_realtime_api.py tests/test_architecture_boundaries.py tests/test_observation_lanes.py tests/test_observation_reviews.py tests/test_ambient_perception.py tests/test_structured_perception.py tests/test_interaction.py tests/test_routed_tools.py
```

本地演示实例使用 8870 端口和 `artifacts/modular-demo-20260914/runtime.json` 独立数据根目录。历史产物位于本工作区，克隆仓库不保证包含这些运行文件。
