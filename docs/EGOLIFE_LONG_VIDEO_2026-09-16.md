# EgoLifeQA 完整长录屏

[播放长录屏](../outputs/egolife-long-video-20260916/index.html) · [MP4](../outputs/egolife-long-video-20260916/egolife-long-benchmark.mp4) · [原始运行](../outputs/egolife-benchmark-long-20260916-r1/report.json)

成片为 716.68 秒，约 11 分 57 秒。这一版使用一次连续浏览器录制，先后运行官方 #81 和 #102。每题独立会话，从头到尾播放本地全部十个视频文件，时间覆盖 DAY1 11:09:42–11:14:30。拼接后长度 287.85 秒，原速播放，每秒约两个画面通过实际系统 WebSocket 输入。两题之间保留真实页面重载和会话切换。

本轮录制和链路核验均通过：每题发送 575 个不同画面，共 1,150 帧。两题均回答“选择：无法确定”，官方答案分别为 #81 的 D（Katrina, Lucia, Shure）和 #102 的 B（小马座椅）；未选择不计为正确。完整输入更多本地素材没有在本轮得到正确的选项，不能据此宣称模型已能完整理解这段视频。

两个回答都使用此前生成的场景观察，日志标记为 `previous-model-observation`。核验确认观察帧来自对应题目的实际输入、回答文字与原始语音一致、预测文件在揭晓标准答案后未变。链路通过不代表模型的场景描述或推断一定正确。

播放验收通过：17,916 帧全片解码、H.264/AAC 1920×1080、非静音音轨、Edge 实际播放、本地文件播放、六个章节逐一跳转，以及 1440/390 宽度下的播放页和展开对比表。代码编译检查与 `git diff --check` 通过。

题干、选项、标准答案均使用官方原文。预测保存后才在录屏中揭晓 Ground truth，不因答错或拒答而重跑。官方题目已在之前短版演示中使用，不是独立测试。Q81 没有提供姓名身份表；Q102 在 11:16:52 的另一个目标时刻不在本地视频内。未输入全天历史，允许无法确定，因此不能把本演示的结果称为官方 benchmark 得分。

导出只裁去正式开始前和最后一题结束后的准备片段，两题之间没有剪切，也没有倍速。视频原始环境音未输入；题目通过文字提交，合成音用于朗读，助手声音为本轮真实模型输出，按照音频首包日志近似对齐。观看视频期间没有人为补上旁白或假造回答。播放页提供六个跳转点，分别定位两题的视频输入、题目朗读与提交、标准答案对比。

完整本地视频的十个源文件及哈希保存在 `outputs/egolife-full-local-media/manifest.json`。逐帧时刻与哈希、观察来源、turnId、responseId、固定预测、标准答案字母对比都可通过成片旁的链路核验查阅。回答实际使用的视觉路径以日志为准，不能把连续接入全部画面视为回答模型直接读过每一帧或具有完整长视频记忆。

## 复现

```powershell
.\.venv\Scripts\python.exe scripts/run_egolife_benchmark_demo.py --output outputs/egolife-benchmark-long-new --username benchmark-long-new --full-local-video --media outputs/egolife-full-local-media/egolife-full-local.mp4
.\.venv\Scripts\python.exe scripts/build_egolife_long_video.py --source outputs/egolife-benchmark-long-20260916-r1
.\.venv\Scripts\python.exe scripts/verify_egolife_long_video.py --output outputs/egolife-long-video-20260916
```

录制脚本默认连接本地 8775 端口的真实系统。`--full-local-video` 与原来的一分钟片段模式分开，原有短视频及其预测不被覆盖。导出器要求两个题目完整成功执行、全视频覆盖以及超过十分钟的连续录屏；“执行成功”仅指链路完成，不代表答案正确。
