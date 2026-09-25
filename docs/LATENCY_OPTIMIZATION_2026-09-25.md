# 交互延迟优化（2026-09-25）

本次在 JEV v8 路由上优化传输和语音准备，不改变模型判断、记忆检索范围、取消复核或说话结束阈值。改动在源码默认启用；下列结果来自正式启用前的隔离应用验证。

后续针对长会话的重复上下文及状态复制，见[长会话延迟优化](LONG_SESSION_OPTIMIZATION_2026-09-25.md)。下文数据保留本阶段原始结果。

## 改动及依据

- JEV HTTP 连接池由应用持有，跨实时连接复用；默认启动时预建 4 条连接，闲置保留 60 秒，池上限 32。预建只向同一供应商公开 count 接口 GET，不发送凭据、用户内容或推理请求；不跟随重定向、不缓存 cookie。准备失败不阻止服务启动，实际分类仍做完整错误检查。路由对象、上下文、密钥和熔断仍各自隔离，关闭一个 WebSocket 不关闭整个应用连接池。
- 请求审计新增 `transport`：DNS、连接、发送请求头、收到响应头和是否复用连接。原密集输入中建连约 470–545 ms，复用后同类请求约 370 ms；主要瓶颈是重复建连，不能称为 JEV 推理本身花了一秒。
- 已通过路由许可的回答，允许文字生成与 TTS 连接并行。语音 session.updated 之前不提交文本，等待期间最多预取一个文字片段；取消同步释放生产任务。文字前台链路原本已有独立生成任务，因此这部分收益取决于实际输入路径，不能把总体收益全部归因于 TTS。

## 交替真实对照

[冻结计划](../outputs/latency-ab-20260925-r1/plan.json) / [完整结果](../outputs/latency-ab-20260925-r1/report.json)：相同 v8 提示词、应用、模型与三个计算问题，每组间隔 0/200/500 ms，baseline/optimized 顺序交替，重复 3 次，各 27 个真实问题。baseline 关闭共享连接池和 TTS 预取，optimized 打开；输出哈希一致，没有事后改写分数。

| 指标 | baseline | optimized |
| --- | ---: | ---: |
| 正确且持久化的答案 | 27/27 | 27/27 |
| 回执中位数 | 40.9 ms | 40.2 ms |
| 路由中位数 | 1008.1 ms | 498.5 ms |
| 最新问题首音中位数 | 2155.2 ms | 1552.0 ms |
| 应用冷启动后首组首音中位数 | 2677.7 ms | 2571.5 ms |
| 同一应用后续两组首音中位数 | 2139.75 ms | 1547.05 ms |

首音中位缩短 **28.0%**，路由中位缩短 **50.6%**。27 次路由中复用连接次数从 2 增至 18。启动后立即发三条请求时，预建连接可能尚未完成，仍需新建连接；没有等待预热后才开始计时，也没有隐藏冷启动成本。

这只是 3 个已知问题的重复开发测量，不能当成 p95、吞吐量或泛化质量结论；网络和供应商仍会波动。两项优化一起开关，没有证明各自的独立贡献。9 个最新问题首音样本与 27 个路由样本口径不同。

## 功能与边界

- [全量回归](../outputs/latency-optimization-regression-20260925.xml)：846/846，通过耗时 107.448 秒，3 条既有 aiohttp 弃用提示。新增本地 HTTP/WebSocket 测试验证：共享池不混用凭据/cookie/熔断、单路由关闭不伤其他连接、应用退出关闭池、预取不提前送文本且取消后不漏音频。
- [真实记忆](../outputs/latency-memory-20260925-r1/acceptance.json)：随机编号保存、新会话回忆并继续使用，通过。
- [真实交互控制](../outputs/latency-controls-20260925-r1/acceptance.json)：文字续答、声音续播、定向取消、静默重连，4/4 通过。
- [浏览器语音](../outputs/latency-voice-browser-20260925-r1/acceptance.json)：本地 Silero 停播约 212.2 ms，续播保留原游标，新问题正常播放，静默刷新后保留，1440/390 布局通过，无页面异常。该单次停播值与前轮约 160 ms 的差别不能作为性能回退结论。
- [长会话](../outputs/latency-long-20260925-r1/report.json)：9193 行同一历史副本，全部回答和预算通过。两轮文字首音 4615.2 / 4028.4 ms，语音结束至首音 4405.5 ms，连续两条持久化回答完成约 6895.6 ms。相比此前 4589.4 / 3754.9 / 4537.2 ms，没有明显的整体改善，不能把新会话的 28% 外推到长会话。

没有缩短语音结束静默窗口，没有通过跳过记忆读取或取消复核换取低延迟。已有复杂时间记忆失败仍未解决。所有验收使用真实供应商；语音为合成 PCM，不是物理麦克风声学测量。

## 配置与复现

```dotenv
EGOASSIST_DECISIONS_CONNECTION_POOL=1
EGOASSIST_DECISIONS_PRECONNECT=4
EGOASSIST_TTS_PREFETCH=1
```

`CONNECTION_POOL=0` 恢复每个路由对象自有连接；`PRECONNECT=0` 只保留复用、不预建；`TTS_PREFETCH=0` 恢复原等待握手的路径。应用池仅在 `EGOASSIST_TURN_ROUTER_BACKEND=openrouter-decisions` 启用。生产配置变更需重启加载。

```powershell
.\.venv\Scripts\python.exe scripts/compare_rapid_latency.py --output outputs/latency-ab-new --repeats 3
.\.venv\Scripts\python.exe scripts/verify_real_interaction.py --output outputs/latency-controls-new
.\.venv\Scripts\python.exe scripts/verify_real_memory.py --output outputs/latency-memory-new
```

本地保留 trace/pool 探测及全部运行结果，`outputs/` 不提交 Git，未发布密钥或私有历史内容。
