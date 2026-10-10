# EgoAssist

面向智能眼镜的第一视角助手：本地 Python 服务 + 浏览器控制台。浏览器提供文本、麦克风和摄像头输入，服务端负责实时对话、现场观察、长期记忆和日记回顾。Qwen（DashScope）提供识别、理解、回答与报告；可选 JEV（OpenRouter）承担轻量路由与控制判断。

项目同时是记忆研究的工程基座。新的机制对照与评分在独立项目 MemoryMechanismLab 中进行，两者分开构建与发布，见[产品与科研边界](docs/RESEARCH_BOUNDARY.md)。

> 软件测试通过不代表模型判断正确，也不代表眼镜硬件已经验收。

## 快速开始

环境：Windows + Python 3.13 + Edge 为主要验证环境；云端 Demo 不需要 GPU。以下命令在项目根目录（本机为 `D:\DEMO\EgoAssist`）的 PowerShell 中执行。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,modular]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在 `.env` 中填写 `QWEN_API_KEY`（或 `DASHSCOPE_API_KEY`），账户需有对应模型权限，网络需能访问 DashScope 的 HTTPS 与 WebSocket。**不要提交 `.env`。**

```powershell
.\.venv\Scripts\python.exe .\run_egoassist.py --open
```

`--open` 会等服务开始监听后再打开浏览器。终端出现 `Running on http://127.0.0.1:8770` 即可访问；按 `Ctrl+C` 停止。首次使用先注册账户并创建案例，麦克风和摄像头需要浏览器授权。Linux/macOS 用 `.venv/bin/python`（未全面验证）。

| 页面 | 地址 |
| --- | --- |
| 助手：语音、摄像头、任务 | <http://127.0.0.1:8770/> |
| 个人日记：日报、周报、月报 | <http://127.0.0.1:8770/diary.html> |

完整功能演示（统一检查、独立演示数据、四个 Skill、七个任务入口）见[演示指南](docs/DEMO_GUIDE.md)：

```powershell
.\.venv\Scripts\python.exe scripts/demo.py check
.\.venv\Scripts\python.exe scripts/demo.py start
```

## 架构

三条链路各自执行，通过带来源的事件协作：

| 链路 | 职责 | 核心代码 |
| --- | --- | --- |
| 观察 | 采样声画、理解现场、校验证据 | `realtime/observation_service.py`、`realtime/scene.py` |
| 交互 | 理解输入、打断与补答、执行 Skill 和工具 | `realtime/turn_coordinator.py`、`realtime/interaction_service.py` |
| 记忆 | 原文证据、派生记忆、时间检索、纠正与遗忘 | `memory/evidence_service.py`、`memory/timeline.py`、`memory/commit.py` |

发言时机由语义控制：附和与旁人交谈不打断当前回答，新问题和停止指令才切换。见[日常对话语义交接](docs/DAILY_DIALOGUE_2026-09-26.md)。

| 目录 | 定位 |
| --- | --- |
| `egoassist/application/` | 服务装配、HTTP/WebSocket 接口、账户与案例生命周期 |
| `egoassist/core/` | 无上层依赖的共享契约、来源日志、删除规则 |
| `egoassist/realtime/`、`egoassist/memory/` | 三条核心链路 |
| `egoassist/product/` | 持久作业、隐私、提醒、外部连接器 |
| `egoassist/diary/` | 个人日记与日/周/月分层压缩 |
| `egoassist/console/` | 原生 JS 控制台（资源随包分发，不依赖 CDN） |
| `egoassist/skills/`、`prompts/`、`watchlists/` | 政策与资源的唯一来源 |
| `egoassist/capture/`、`backends/`、`qa/`、`retrieval/`、`abstention/`、`eval/` | 离线视频记忆研究，不在实时主流程 |
| `tests/`、`scripts/`、`docs/` | 回归测试、诊断与验收脚本、架构与实验记录 |

运行形态为本地单进程。详见 [ARCHITECTURE.md](ARCHITECTURE.md) 与 [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md)。内置 Skill：`kitchen-coach`、`assembly-coach`、`packing-coach`、`field-notes`。

## 记忆

原话先落盘为原文证据，再按策略提取事实、偏好与事件，写成 Timeline 卡片（SQLite）。回答时按用户、案例、时间范围检索并注入带来源的证据；后台负责失败恢复、长事件整理与日记压缩。

- **检索**：默认是词法检索——三字 trigram 全文索引加一个能匹配两字中文词的词项索引，打分前先过范围、时间、主体等硬过滤，最多评分 256 个候选（`EGOASSIST_MEMORY_CANDIDATE_LIMIT`）。
- **可选语义通道**：`EGOASSIST_MEMORY_EMBEDDER=dashscope` 启用向量召回（text-embedding-v3）。它只能提名候选、救回零词面重叠的改写，不会绕过任何硬过滤；向量随卡片一起删除。默认关闭。
- **指代策略**：`EGOASSIST_MEMORY_REFERENCE_POLICY` 控制只有代词、没有主体的卡片能否作答——`strict`（默认，不作答）、`demote`（降权）、`off`。
- **记忆单元**：带起止区间；离线 EgoLife 实验可把字幕合并为 ≤30 秒的事件单元，并带结构化说话人。
- **分层**：长事件写成 long 层卡片；`build_digests` 把相邻卡片聚合成摘要卡，子卡被删除或更正时摘要卡一并作废。
- **待确认记忆**：推断得来的事实先挂起，可通过 `GET/POST /api/memory/pending` 确认或拒绝。
- **删除**：关闭记忆、删除卡片和擦除原始来源的影响范围不同，详见记忆文档。

当前历史问答开发集证据召回为 **72.82%**（目标 80%，未达到），见[证据架构与校正结果](docs/SOURCE_EVIDENCE_LIFECYCLE_FIX_2026-09-17.md)。2026-10-09 的建库修复与 EgoLife 30 题检索消融见 [TIMELINE_BUILD_FIX](docs/TIMELINE_BUILD_FIX_2026-10-09.md)：改用事件单元后候选池命中 4/30 → 18/30、Top-5 命中 0 → 4/30，其中一部分来自每张卡覆盖时长变长，引用时需一并说明。各实验协议不混合排名，见[历史实验索引](docs/HISTORY.md)。

## 配置

全部配置见 [.env.example](.env.example)，依赖以 [pyproject.toml](pyproject.toml) 为准。

| 设置 | 作用 |
| --- | --- |
| `QWEN_API_KEY` / `DASHSCOPE_API_KEY` | 必填，云端模型 |
| `EGOASSIST_REALTIME_BACKEND` / `--realtime-backend` | `modular`（默认：ASR + VL + 文本 + 流式 TTS）或 `omni` |
| `EGOASSIST_TURN_ROUTER_BACKEND` | `qwen`（默认）或 `openrouter-decisions`（JEV 优先，另需 `OPENROUTER_API_KEY`） |
| `EGOASSIST_MEMORY_REFERENCE_POLICY` | `strict` / `demote` / `off` |
| `EGOASSIST_MEMORY_EMBEDDER` | `none` / `hashing`（离线测试用，非语义）/ `dashscope` |
| `LIVEKIT_URL` / `_API_KEY` / `_API_SECRET` | 配齐后媒体走 LiveKit RTC，否则走 WebSocket |
| `EGOASSIST_TEMPORAL_TARGET` | 提醒功能所需的 Temporal 服务 |

后端对照见[模块化实时后端](docs/MODULAR_REALTIME_2026-09-14.md)与[语音后端对照](docs/VOICE_BACKEND_COMPARISON_2026-09-26.md)；JEV 启用记录见[长会话优化](docs/LONG_SESSION_OPTIMIZATION_2026-09-25.md)。离线模型研究另装 `.[gpu,api]`，PyTorch 按平台单独安装。

第二个实例必须同时更换端口和数据根目录（每个根目录只允许一个进程持有写入租约）：

```powershell
.\.venv\Scripts\python.exe .\run_egoassist.py --port 8780 --memory-path .egoassist-second/runtime.json --open
```

## 故障排查

**页面打不开**：等终端出现 `Running on` 后刷新，不要重复启动。端口或数据目录被占用时，检查是否已有实例在运行。日志在 `.egoassist/server.log`：

```powershell
Invoke-WebRequest -Uri http://127.0.0.1:8770/ -UseBasicParsing | Select-Object StatusCode
Get-Content .\.egoassist\server.log -Tail 40
```

返回 `200` 只说明页面可访问，不代表模型、麦克风或摄像头已经连通。

**实时连接断开**：浏览器会自动重连（最多 6 次，退避约 15 秒）。服务端会等旧连接关闭完成后再接入新连接，不再因为重连过快而返回 409。同一会话若已在另一个标签页打开，第二个连接仍会被拒绝——这是"一个会话一条连接"的规则。

**模型拒绝访问**：`Arrearage` 表示账户欠费，请在阿里云百炼检查计费；`AccessDenied` 还需核对密钥和模型权限。账户恢复后重新建立实时连接。解析失败的输入不会丢，可在提示词审计的记忆恢复记录中核对原话后点"重试解析"。见[实时异常处理记录](docs/LIVE_FAILURE_REMEDIATION_2026-09-14.md)。

## 测试

```powershell
# 确定性回归，不需要 API Key
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider

# 浏览器验收（Edge + Playwright，默认不调用云端回答）
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe scripts/verify_browser.py --output outputs/new-browser

# 真实供应商验收，每次使用新目录保留证据
.\.venv\Scripts\python.exe scripts/verify_real_turn_attribution.py --output outputs/new-attribution
.\.venv\Scripts\python.exe scripts/benchmark_realtime_latency.py --output outputs/new-latency --repeats 3
```

2026-10-10，Python 3.13 / SQLite 3.53 下全量 1083 通过、3 跳过。跳过的是 3 个需要 Temporal 测试服务器的用例：本机未下载该二进制时自动跳过，联网运行一次后即可执行。需要 Node 的控制台测试在没有 Node 时同样跳过。公共测试工具在 `tests/support.py`。

离线评测：`run_egolife_benchmark.py --help`；EgoLife 检索消融：`scripts/egolife_independent_modes.py retrieve --unit event|cue`；记忆机制对比：`scripts/compare_memory_mechanisms.py --help`。

清理旧测试目录与缓存（先预览，加 `-Apply` 才执行；不会删除账户数据、媒体和实验日志）：

```powershell
.\scripts\clean_workspace.ps1
.\scripts\clean_workspace.ps1 -Apply
```

## 部署与数据

默认只绑定 `127.0.0.1`。远程访问需要 TLS、访问控制和持久卷。

```powershell
docker build -t egoassist .
docker run --rm -p 127.0.0.1:8770:8770 --env-file .env -v egoassist-data:/app/.egoassist egoassist
```

Docker 配置存在，但容器部署尚未验收。备份要先停止写入，再复制整个数据根目录（SQLite、日志、媒体），不能只复制 `runtime.json`。账户支持导出与删除。`.egoassist*/`、`outputs/`、`data_real/` 是数据，不是可以随手清理的残留。

## 已知限制

- 模型可能遗漏信息、指代错误、误解时间或漏检证据。
- 采样图片会保存，连续的原始音视频不完整保存。
- 刷新页面能恢复文本与任务，不能恢复旧的媒体连接和音频缓存。
- 重启后持久任务会恢复，但不保证云端调用或外部副作用恰好执行一次。
- 眼镜硬件、长时间连续运行、功耗、多设备同步和生产灾备均未验收。

[开发约束](CONTRIBUTING.md) · [安全说明](SECURITY.md) · [Apache 2.0](LICENSE) · [第三方归属](NOTICE) · [文档导航](docs/README.md)
