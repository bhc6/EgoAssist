# EgoLife 连续视频与语音实录 · 2026-09-16

[播放页](../outputs/egolife-multimodal-video-20260916/index.html) · [MP4](../outputs/egolife-multimodal-video-20260916/egolife-multimodal-conversation.mp4) · [链路索引](../outputs/egolife-multimodal-video-20260916/manifest.json) · [逐轮画面和语音核对](../outputs/egolife-multimodal-20260916-r2/multimodal-evidence-check.json)

本次用连续移动的 EgoLife 第一视角视频重新运行系统，8 轮语音问答，导出约 1 分 54 秒视频。录屏同时显示正在播放的原视频、帧计数、语音问题和实际回答。画面在问答期间持续播放，没有用静态图片冒充视频。视频结束后保留 4 秒阅读停留。

素材来自本机 `data_real/egolife/A1_JAKE_DAY1` 中从 `DAY1_A1_JAKE_11100000.mp4` 到 `DAY1_A1_JAKE_11140000.mp4` 的 9 段连续文件，原速拼接为 `outputs/egolife-multimodal-media/egolife-continuous.mp4`，实际使用前约 110 秒。原视频的环境音关闭；演示问题由 Qwen TTS 生成，实际通过系统 WebSocket 音频输入与 ASR，不能称为真人麦克风问答或视频原声理解。

220 个不同画面以 2 FPS 通过真实 `video` 消息进入系统，8 轮均有与输入帧哈希相符的场景快照、语音证据、真实语音回答及一致的回答文本。独立核验逐轮关联 turnId、responseId、sourceSeconds、图像哈希与音频哈希。连续接入的全部帧不等于每帧都被模型完整理解；回答使用当轮可用的视觉证据，不能推断已经实现完整长视频理解。

问答围绕当前场景、桌面物品、桌布颜色、手部动作、视野连续性、小字可读性和不确定细节展开。模型实际回答原样保留：例如桌布为红白格子，小字无法读清，在场人员身份、关系与讨论内容不确定。场景、物品和动作推测仍需人工对照画面；链路通过不代表所有视觉细节均准确。

## 实际问题与修复

首轮 `egolife-multimodal-20260916-r1` 暴露语音片段 ID 对齐问题：服务的 VAD start 与 stop 有时使用不同 item ID，旧实现让已结束片段滞留为待转写，从而持续触发 unresolved-user-input，回答转为后台文字，有一轮没有关联画面。原始失败报告、事件与录屏保留。

现在 stop 优先按 item ID 匹配；仅在唯一未结束区间对应当前 active segment 时，接纳 stop 的最终 item ID，并保留 startProviderItemId。旧片段迟到的 stop 不再关闭新片段。增加回归验证 ID 变更后待处理状态清空，以及乱序 stop 不影响新输入。相关 27 项检查及完整测试套件通过，完整套件有 3 条既有 aiohttp 弃用警告。

演示问题改为自然的单句提问，避免 TTS 句间停顿被当成独立问题。当前录制不宣称解决了任意多句长语音的分段与汇总问题。

第二轮 `egolife-multimodal-20260916-r2` 的 8 轮音画交互完整成功，但录制脚本额外设定的“至少两分钟”检查失败：实际对话只用了约 110 秒。原始报告的 `passed=false` 保留不改。独立核验 `multimodal-evidence-check.json` 对 8 轮全部验证通过；导出器只允许这一项时长检查失败，任何功能检查失败仍拒绝导出。后续录制把连续输入覆盖门槛改为一分钟，没有延长或倍速修改本次画面来凑时长。

## 复现

```powershell
.\.venv\Scripts\python.exe run_egoassist.py --port 8775 --memory-path .egoassist-egolife-demo/runtime.json
.\.venv\Scripts\python.exe scripts/run_egolife_multimodal_demo.py --base-url http://127.0.0.1:8775 --data-root .egoassist-egolife-demo --output outputs/egolife-multimodal-new --username egolife-new
.\.venv\Scripts\python.exe scripts/verify_egolife_demo_evidence.py --source outputs/egolife-multimodal-20260916-r2
.\.venv\Scripts\python.exe scripts/build_egolife_multimodal_video.py --source outputs/egolife-multimodal-20260916-r2
.\.venv\Scripts\python.exe scripts/verify_story_videos.py --output outputs/egolife-multimodal-video-20260916
```

导出使用同一轮原始回答音频，按日志首包时间近似对齐，不是重新生成的助手配音；原始浏览器录屏没有系统音轨。视频为 H.264/AAC、1920×1080。检查涵盖全帧解码、非静音音轨、Edge 实际播放和 1440/390 宽度布局。
