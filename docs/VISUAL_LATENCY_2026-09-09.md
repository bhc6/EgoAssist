# 视觉问答延迟优化

## 原因与修改

旧版文字视觉问答先等待 `qwen3-vl-flash` 完整描述画面、人物、位置等，再由实时模型回答。问桌布颜色时仍生成约 300 Token 的完整场景 JSON，该阶段实测 3.4–4.2 秒，遮蔽了并行运行的语义路由收益。

新版 `VisualQuestions` 用提问时保存的同一张原图，只提取当前问题相关的视觉证据。它与语义路由并行，不等待后台场景描述的请求或并发名额。实时回答仍经过原来的交互控制、任务/记忆权限、响应调度和打断处理。完整场景及人物描述在原后台路径继续更新。

提示词位于 `egoassist/prompts/scene.json` 的 `question`，不包含桌布或测试答案。回答长度没有通过前端裁剪来缩短。前台证据有 256 Token 上限，返回截断时标记失败，不当作完整证据；默认 4 秒截止包括排队和请求，适合短视觉问题，长文本识读不在本次性能保证内。失败仍保留原始图片，原图后台补答路径未改动。

每个问题有独立请求、结果与原图引用；同一问题重复等待共用请求。取消等待不会污染其他问题或阻塞语音控制。前端可检查 `scene.question.request/result`，查看实际提示词、原图哈希、供应商 requestId 和耗时。完整场景不会被某一问题的局部证据覆盖。

## 同条件真实对照

EgoLife `DAY1_A1_JAKE_11100000.mp4` 的同一帧；同一问题“请描述摄像头中桌布的颜色和纹理，不要猜测姓名。”；同一实时模型、Qwen key 和输出链路。每次使用独立会话，新旧路径交替运行各三次。六次均返回相符的红白格纹描述，没有模拟模型响应。

| 指标，中位数 | 旧版完整场景前置 | 新版按问题取证 |
| --- | ---: | ---: |
| 视觉证据生成 | 3815.5 ms | 495.1 ms |
| 提交到首字 | 4417.2 ms | 1241.5 ms |
| 提交到首音频 | 4674.0 ms | 1500.2 ms |
| 提交到完整回答 | 6259.0 ms | 2920.4 ms |

首音频等待减少约 68%，完整回答等待减少约 53%。新版三次首音频为 1436.4 / 1500.2 / 1584.2 ms，完整回答为 2308.2 / 2920.4 / 2957.4 ms。客户端单调时钟从发送文字 WS 消息计时，包含路由、视觉、响应调度和输出接收，不包含预先建立连接及物理扬声器播放。

原始证据：`outputs/visual-latency-ab-1/report.json`，以及该目录 `users/*/sessions/*.jsonl` 中的全部供应商请求、输出和状态。

最终真实浏览器复验：`outputs/visual-focused-browser-final/report.json`，视觉问题服务端接收到首字 1667 ms、首音频 1941 ms、完整生成 2866 ms；浏览器测试观测到持久历史更新为 3596.6 ms，包含交互操作和状态刷新等待。字幕与音频仍通过 WS 流式输出，不能把状态刷新的时间误记成模型生成时间。

## 验证与限制

- 413 项自动测试通过。新增测试覆盖慢后台视觉不阻塞前台、重复请求合并、不同问题结果隔离、取消等待、排队截止和逐问题端到端指标。
- 真实浏览器覆盖文字后开启摄像头、实际场景问答、EgoLife 音频回放、声画对齐、页面刷新和移动端/桌面布局；无页面异常，25 条历史滚动检查通过。
- 阿里云原生图片消息曾进行独立探索：有的格式报 Invalid video file，有的只成功返回文字但内容错误；**未将这些路径接入应用**。失败/探索记录保留在 `outputs/realtime-image-probe-*`；其中第 2 次报告的早期 `passed` 只检查输出存在，人工内容复核失败，不能算视觉验收通过。
- 三次短样例不是 P95 或复杂场景准确率评估，不是眼镜硬件实测。当前主要等待转为语义路由和实时回答生成；多人归属、复杂 OCR、弱网络仍需专项评估。

## 配置与复现

无需新增 API key。默认 `EGOASSIST_VISUAL_QUESTION_MODE=focused`、`EGOASSIST_VISUAL_QUESTION_MODEL=qwen3-vl-flash`、`EGOASSIST_VISUAL_QUESTION_TIMEOUT=4`。`scene` 模式仅用于复现旧路径或排查。原始证据、背景观察和隐私导出规则保持不变。

```powershell
.venv\Scripts\python.exe scripts/verify_visual_latency.py --rounds 3 --output outputs/my-visual-ab
.venv\Scripts\python.exe scripts/verify_shared_scene_browser.py --output outputs/my-visual-browser
.venv\Scripts\python.exe -m pytest
```
