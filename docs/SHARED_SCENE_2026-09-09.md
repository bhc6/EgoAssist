# 声画关联与界面验收

后续延迟优化已用按问题提取的视觉证据替代前台完整场景描述。本文 4.8–6.0 秒属于旧路径历史数据；新旧真实对照及最新代码行为见 [视觉延迟优化](VISUAL_LATENCY_2026-09-09.md)。

## 当前能力与边界

摄像头观察独立于 Skill。文字提问会保存当时的 JPEG，视觉描述与语义路由并行，随后供实时模型回答；原问题的后台补答直接携带保存的原始图片调用视觉模型。语音提问使用实时音视频通道，不等待额外的场景描述。静音、取消等控制不等待视觉请求。

现场声音使用本地 sherpa-onnx 的 pyannote 分段和 3D-Speaker CAMPPlus 声纹，得到匿名声音编号。多声音片段再异步调用真实 Qwen ASR，按时间段保留原音频和转写。不同声纹簇不得合并成同一人；存在丢弃短段、多簇或模糊匹配时，不将整句归为佩戴者。重叠语音不强行逐字分配。

声纹相似度是匹配量，不是身份正确率。姓名和“这是我的声音”由登录账号确认。画面人物描述与声音分离保存，画面关联只覆盖确认的参考帧；**没有实现自动唇动关联、持续人脸跟踪或可靠的多人声画身份识别**。EgoLife 样例人物面部有遮挡，不能从这些样本声称验证了主动说话人定位。声音合成测试也不是现场识别准确率评估。

未确认的现场声音可以作为对话证据；语义模型判断是否对助手说话，不按中文关键词触发。个人记忆修改、任务控制和回答取消受说话人边界限制。声纹不是安全认证，不能用于支付等敏感授权。

## 修复记录

- 纠正“没有任务检查点视觉结果就看不到摄像头”的错误提示；修复文字连接后启用摄像头未传图。
- 音频按连接代次和供应商音频位置截取；重连回放的前缀计入位置偏移，旧连接不能使用新连接音频。
- ASR 使用供应商 `item_id` 对应片段，乱序结果不会按先来后到分配说话人。
- 音视频按服务端实际接收时间保留对齐帧。500 ms 是匹配窗口设置，**不是实测同步精度**；没有设备时钟校准保证。
- 没有对齐帧时不借用 ASR 完成时的新画面。内容哈希验证原始媒体，背景回答不改用后来的图片。
- 视觉请求按帧复用，最多两路并行；音频模型在工作线程运行，与语义路由并行。分段转写不占前台回答路径。
- 前端刷新不会取消已开始的分段转写；进程中断留下的未完成分段在重新打开案例时恢复。未接收的实时媒体不具备恢复保证。
- 媒体读取接口增加身份和案例归属检查；原始音频、帧和声纹属于账户导出/删除范围。

## 前端

文字输入位于视频下方。所有已持久化的问题和回答按时间排列，滚动容器不再只显示最后 8 条，后端也不再截断为 20 条。用户阅读历史时刷新状态不跳回底部。

旧的全局 Pipeline 阶段推测与隐藏组件已删除。“这句话如何处理”按问题显示当前进度、声音来源、帧与音频证据、语义分类、记忆限制、实际回答模型和测量耗时。任务显示正在做的事项；结果正文直接展示，原始 JSON、调度细节和时间线折叠。录音播放器不会因状态刷新重建。

## 真实验证

所有报告保留各自输出目录，不覆盖失败尝试。没有模拟供应商响应。

| 运行 | 结果 | 证据 |
| --- | --- | --- |
| 真实 EgoLife 12 秒片段，本地分段、Qwen 视觉、携带原图补答 | 通过；本地冷启动分析 899.6 ms | `outputs/shared-scene-real-2/report.json` |
| 两种真实 Qwen TTS 声音，混合后本地分段与独立 Qwen ASR | 两簇，三段转写完成；本地 912.1 ms，ASR 381.1 / 450.9 / 900.5 ms | 同上，包含每次供应商 requestId |
| 第一次真实浏览器验收 | 失败，发现媒体接口缺少 re 导入；颜色断言对红/橙色边界过窄 | `outputs/shared-scene-browser-1/report.json` |
| 修复后真实浏览器与实际音频传输 | 通过，文字后开摄像头、桌布问答、音频/画面对齐、刷新恢复 | `outputs/shared-scene-browser-2/report.json` |
| 连接时钟与模型标签修复后真实浏览器复验 | 通过；文字问题到完整答案 2216.1 ms，视觉问题 4798.9 ms | `outputs/shared-scene-browser-3/report.json` |
| 删除旧 Pipeline 后浏览器 UI 回归 | 通过，无页面异常或失败请求，1440/390 px 均无横向溢出 | `outputs/shared-scene-ui-final/acceptance.json` |
| 最终版本真实浏览器、声画与完整历史复验 | 通过；普通文字问题 3446.8 ms，视觉问题 6004.3 ms；首音频 782.0 ms（仅响应阶段）；没有超时 | `outputs/shared-scene-browser-final/report.json` |

最终自动回归为 **410 passed**（61.46 秒）；另有现存 aiohttp API 弃用、AppKey 和 pytest 缓存目录权限警告。覆盖媒体归属/导出/删除、ASR 乱序、跨代音频、回放前缀偏移、混合声纹、未确认声音权限和超过 20 条历史保留。有限真实样例中的视觉问答完成时间为约 4.8–6.0 秒，不能替代长期延迟分位数。

第三次浏览器运行中，视觉问题响应请求到首音频为 711.7 ms；这**不包含前面的视觉与语义处理时间**。首次场景描述 3319.6 ms，路由约 0.6–1.2 秒，说明视觉描述仍是文字视觉问答的主要等待项。真实音频两段说话人分析为 821.4 ms（模型初始化）和 59.5 ms（已热身）。样本太少，不能当成 P95、跨网络承诺或眼镜实测延迟。

摄像头为真实数据集图片生成的浏览器媒体流，麦克风链路回放真实数据集 PCM；没有测量物理摄像头、佩戴麦克风、扬声器回声或多人唇动同步。25 条历史滚动测试使用明确标注的 UI 数据，不伪装成模型结果。

## 环境与复现

```powershell
.venv\Scripts\python.exe -m pip install -e ".[perception]"
.venv\Scripts\python.exe scripts/setup_shared_perception.py
.venv\Scripts\python.exe scripts/verify_shared_scene.py --output outputs/my-scene-run
.venv\Scripts\python.exe scripts/verify_shared_scene_browser.py --output outputs/my-browser-run
.venv\Scripts\python.exe -m pytest
```

需要现有 `QWEN_API_KEY`，无需新增 API。模型为 `qwen3-vl-flash`、`qwen3-asr-flash`、`qwen3.5-omni-flash-realtime`。浏览器脚本使用已安装的 Edge 和 Playwright。

本地默认 CPU 两线程，不依赖 CUDA；安装包可选依赖为 sherpa-onnx 1.13.7。脚本从 sherpa-onnx 公共发布下载 pyannote segmentation 3.0 与 CAMPPlus 中文声纹模型，验证固定 SHA256，保存在 `.cache/shared-perception`。来源 URL 和校验值在安装脚本中。上游说明：[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)、[3D-Speaker](https://github.com/modelscope/3D-Speaker)、[pyannote segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)。模型权重的使用条件以各上游模型说明为准；本仓库未重新许可这些权重。

可调项：`EGOASSIST_SCENE_MODEL`、`EGOASSIST_SCENE_INTERVAL`、`EGOASSIST_SPEAKER_MODELS`、`EGOASSIST_SPEAKER_MATCH_THRESHOLD`。全部新增模型提示词位于 `egoassist/prompts/scene.json`，实际请求与输出可在提示词审计中查看。

## 数据保留

连续音频仅有 90 秒内存环形缓冲；已接收问题对应的 WAV、观察和问题对应的 JPEG 会持久保存。没有保存完整全天视频。原始证据位于 `users/<user>/sessions/media/<session>/`；日志中保留 SHA256、时间、模型、供应商请求和确认记录。账户导出包含二进制媒体 Base64，删除账户会停止该账户运行并删除媒体。数据库保留期限不是媒体自动删除策略。

完整历史当前随状态返回，长时间案例的响应体会增长。硬件部署前仍需评估增量历史分页、设备端采集时钟、回声与重叠语音、已标注多人测试集和场景描述成本；这些没有由本轮短样例验收覆盖。
