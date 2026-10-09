# EgoAssist

面向智能眼镜助手的本地软件 Demo 和记忆研究项目。浏览器提供文本、麦克风和摄像头入口，Python 服务管理案例与持久状态。JEV 可承担有限路由与控制判断，Qwen 提供复杂提取、实时回答和报告生成。

EgoAssist 可作为科研工程基座；新的机制对照与评分由独立项目 MemoryMechanismLab 管理，两者分别构建和发布。见[产品与科研边界](docs/RESEARCH_BOUNDARY.md)。

## 核心

| 链路 | 职责 | 核心代码 |
| --- | --- | --- |
| 观察 | 采样声画、理解现场、校验证据 | `realtime/observation_service.py`、`realtime/scene.py` |
| 交互 | 理解输入、打断与补答、执行 Skill 和工具 | `realtime/turn_coordinator.py`、`realtime/interaction_service.py` |
| 记忆 | 原文证据、派生记忆、时间检索、纠正及遗忘 | `memory/evidence_service.py`、`memory/source_index.py`、`memory/commit.py` |

三条链路独立执行，通过带来源的事件协作。Skill、工具和报告是链路内能力。软件测试不证明模型判断正确，也不代表眼镜硬件已经验收。

日常对话的发言时机由语义控制：附和和旁人交谈保留当前回答，新问题与停止指令才切换发言状态。连续输入的回答会等待短暂语音间隙，不因收到下一段转写就丢失。机制、EgoLife 失败迭代及真实浏览器验收见[日常对话语义交接](docs/DAILY_DIALOGUE_2026-09-26.md)。

## 启动

云端 Demo 不需要 GPU。主要本地验证环境为 Windows、Python 3.13、Edge。

以下 PowerShell 命令在项目根目录执行，本工作区为 `D:\DEMO\EgoAssist`。首次使用先创建虚拟环境并安装依赖；已经完成安装时可直接运行后面的启动命令。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在 `.env` 填写 `QWEN_API_KEY` 或 `DASHSCOPE_API_KEY`，需要对应模型权限以及 DashScope HTTPS/WebSocket 网络访问。不要提交密钥。

本工作区正式使用 JEV 优先路由：另配 `OPENROUTER_API_KEY`，并设置 `EGOASSIST_TURN_ROUTER_BACKEND=openrouter-decisions`。JEV 连接复用、TTS 准备并行及长会话上下文精简默认启用；只配置 Qwen 的新安装仍可使用模板中的 `qwen` 路由。启用结果与限制见[长会话优化及正式启用记录](docs/LONG_SESSION_OPTIMIZATION_2026-09-25.md)。

```powershell
.\.venv\Scripts\python.exe .\run_egoassist.py --open
```

入口文件名是 `run_egoassist.py`，下划线前不要加反斜杠。命令直接使用虚拟环境中的 Python，无需另外激活环境。

`--open` 会等待服务完成初始化并成功监听端口后，再打开默认浏览器。看到终端显示 `Running on http://127.0.0.1:8770` 后，也可直接访问 [http://127.0.0.1:8770/](http://127.0.0.1:8770/)。运行期间保留终端，按 `Ctrl+C` 停止服务。

注册账户并创建案例；媒体设备需要浏览器授权。Linux/macOS 使用 `.venv/bin/python`，不假定所有平台均已验证。

### 日常使用入口

同一服务、同一账户即可使用下面的功能。个人日记无需另外启动服务或注册账号。

| 功能 | 默认本地地址 |
| --- | --- |
| 助手、语音、摄像头与任务 | <http://127.0.0.1:8770/> |
| 个人日记、日报、周报、月报 | <http://127.0.0.1:8770/diary.html> |

日记是独立持久任务与归档模块，共享账户权限和三链路来源。结构化历史导入与问答使用 `/api/history/*`，见[当前功能和实现选择](docs/README.md)。多个端口的旧演示是不同数据环境，日常使用不需要照搬。

### 页面打不开

- 浏览器未自动打开，或之前显示无法连接：等待终端出现 `Running on`，再访问上面的地址或刷新页面，无需重复启动。
- 端口已占用或数据目录已被锁定：先检查是否已有实例运行；需要第二个实例时，同时更换端口和数据根目录，见下方命令。
- 程序报错退出：先看终端错误；默认日志位于 `.egoassist/server.log`。可在另一个 PowerShell 窗口检查服务和日志：

```powershell
Invoke-WebRequest -Uri http://127.0.0.1:8770/ -UseBasicParsing | Select-Object StatusCode
Get-Content .\.egoassist\server.log -Tail 40
```

首页返回 `200` 表示 HTTP 页面可访问，不代表云端模型或麦克风、摄像头已经连接成功。使用自定义端口时，相应修改检查地址。

### 模型拒绝访问或记忆解析失败

- 返回 `Arrearage`：在阿里云百炼检查当前 API Key 所属账户的计费状态，处理欠费或充值。仅有 `AccessDenied` 时，还需检查密钥和模型访问权限。
- 账户限制会停止自动重试和 Omni 重连。恢复账户后，结束并重新建立浏览器实时连接；必要时重启服务。
- 失败输入仍然保留。在提示词审计的记忆恢复记录中，核对原话后点击“重试解析”，不要批量重放可能包含环境语音的历史输入。

已确认的异常、修复与验证见[实时异常处理记录](docs/LIVE_FAILURE_REMEDIATION_2026-09-14.md)。

### 切换模式与多实例

默认使用模块化实时链路，由 ASR、VL、文本模型和流式 TTS 分工。也可显式切回 Omni 对照：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[modular]"
.\.venv\Scripts\python.exe .\run_egoassist.py --realtime-backend modular --open
```

切回使用 `--realtime-backend omni`；配置、真实对照结果和限制见[模块化实时后端](docs/MODULAR_REALTIME_2026-09-14.md)与[语音后端对照](docs/VOICE_BACKEND_COMPARISON_2026-09-26.md)。

第二个实例必须同时使用不同端口和数据根目录：

```powershell
.\.venv\Scripts\python.exe .\run_egoassist.py --port 8780 --memory-path .egoassist-real-demo/runtime.json --open
```

`--memory-path` 的父目录是整个持久数据根目录，每个根目录只允许一个服务进程持有写入租约。

## 结构与边界

| 目录 | 定位 |
| --- | --- |
| `egoassist/application/` | 服务装配、接口、账户和案例生命周期 |
| `egoassist/core/` | 无上层依赖的共享契约、来源日志、增量投影和删除规则 |
| `egoassist/realtime/`、`memory/` | 三条核心链路 |
| `egoassist/product/` | 持久作业、隐私、资料、提醒和外部连接器 |
| `egoassist/diary/` | 个人日记、日/周/月分层压缩、归档和生成恢复 |
| `egoassist/console/` | 原生 JS 控制台与随包分发的媒体/PDF 资源 |
| `egoassist/skills/`、`egoassist/prompts/`、`egoassist/watchlists/` | 政策与资源的唯一来源 |
| `egoassist/capture/`、`backends/`、`qa/`、`retrieval/`、`habits/`、`abstention/`、`eval/` | 离线视频与记忆研究，不是实时主流程 |
| `tests/`、`scripts/` | 行为回归、诊断、真实验收与研究入口 |
| `docs/` | 当前架构、历史决策及实验记录 |

运行形态仍是本地单进程。详细职责见 [ARCHITECTURE.md](ARCHITECTURE.md)，完整记忆机制见 [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md)（2026-09-20 源码核对）。

在线记忆同时使用原文证据索引和 Timeline 卡片：原话先持久化，再按政策提取事实与偏好；回答时按范围检索并注入证据，后台负责恢复、长事件与日记整理。默认检索使用 SQLite FTS/词法匹配，离线分层向量是另一路研究实现。关闭记忆开关、删除卡片和擦除原始来源的影响范围不同，详见记忆文档。

- 实时回答、观察、补答和报告分别执行，仍共享进程、磁盘与网络。
- 文本按账户、案例和请求 ID 去重。重启恢复持久任务，不保证云端调用或外部副作用恰好执行一次。
- 静音、取消、生成完成和播放完成分别记录。刷新可恢复文本与任务，不恢复旧媒体连接或丢失的音频缓存。
- 保留策略暂停该账户写入，清理过期来源及其派生记录；`make clean` 不删除用户数据。
- 状态输出有界、投影增量更新，检索默认最多评分 256 个候选。首次恢复、导出和清理仍受历史规模影响；截断可能影响召回。

内置 `kitchen-coach`、`assembly-coach`、`packing-coach`、`field-notes` 四个 Skill，共七个固定任务及一个无固定步骤工作流。确认检查点不证明实物动作已完成。

## 配置与扩展

配置集中在 [.env.example](.env.example)，依赖以 [pyproject.toml](pyproject.toml) 为准。

- 配齐 `LIVEKIT_URL/API_KEY/API_SECRET` 后使用 LiveKit RTC/API，否则使用 WebSocket；不依赖 LiveKit Agents 框架。
- 提醒需要 `EGOASSIST_TEMPORAL_TARGET`，通知、求助需要真实连接器；MCP 不由默认入口自动连接。
- 离线模型研究安装 `.[gpu,api]`，PyTorch 按平台另装。声画分离模型见 `scripts/setup_shared_perception.py --help`。
- 浏览器资源随包分发，不依赖运行时 CDN。维护使用 `npm ci`、`npm run vendor:livekit`、`npm run vendor:vad`。

## 验证

```powershell
# 确定性回归，不需要 API Key
.venv\Scripts\python.exe -m pytest -p no:cacheprovider
.venv\Scripts\python.exe scripts/benchmark_local_scaling.py

# Edge 浏览器，需要 Playwright；默认不调用云端回答
.venv\Scripts\python.exe -m pip install playwright
.venv\Scripts\python.exe scripts/verify_browser.py --output outputs/new-browser

# 真实供应商，每次使用新目录保留证据
.venv\Scripts\python.exe scripts/verify_real_turn_attribution.py --output outputs/new-attribution
.venv\Scripts\python.exe scripts/benchmark_realtime_latency.py --output outputs/new-latency --repeats 3
```

离线评测用 `run_egolife_benchmark.py --help`，记忆对比用 `scripts/compare_memory_mechanisms.py --help`，综合真实验收用 `scripts/verify_real_acceptance.py --help`。后两者需要相应数据源与供应商访问。

### 质量与历史实验

当前原文历史问答的开发集最终证据召回为 **72.82%**，未达到 80% 目标；结构和引用 ID 校验不等于答案语义正确。见[证据架构与校正结果](docs/SOURCE_EVIDENCE_LIFECYCLE_FIX_2026-09-17.md)。

EgoLifeQA A–D、向量检索、模块化实时后端等对照集中在[历史实验索引](docs/HISTORY.md)。不同协议、数据集合及评分口径不混合排名，未证明更优的研究方案不替换默认运行路径。

### 工作区清理

成功的确定性测试会自动清除本轮生成的临时目录；失败记录和显式 `--basetemp` 保留。Windows 下可预览并清理旧测试目录、Python 缓存与已确认的旧 wheel，默认保留最近三次测试目录：

```powershell
.\scripts\clean_workspace.ps1
.\scripts\clean_workspace.ps1 -Apply
```

清理会生成路径和结果清单，跳过 Git 跟踪文件、链接及无权限目录。账户数据、媒体、模型缓存、原始实验日志和录屏不属于此清理范围。当前说明见[文档导航](docs/README.md)，历史文件的证据链接保留。

## 部署与数据

优先绑定 `127.0.0.1`；远程访问需要 TLS、访问控制和持久卷。

```powershell
docker build -t egoassist .
docker run --rm -p 127.0.0.1:8770:8770 --env-file .env -v egoassist-data:/app/.egoassist egoassist
```

Docker 配置存在不代表容器已验收。备份应停止写入后复制整个数据根目录，包括 SQLite、日志和媒体，不只复制 `runtime.json`。账户支持导出和删除。`.egoassist*/`、`outputs/`、`data_real/` 及模型缓存不是可随意清理的源码残留。

模型仍可能遗漏、错误指代、误解时间或漏检证据。采样图片会保存，连续原始媒体不完整保存。眼镜硬件、持续长时间运行、功耗、多设备同步与生产灾难恢复尚未验收。

[开发约束](CONTRIBUTING.md) · [安全说明](SECURITY.md) · [Apache 2.0](LICENSE) · [第三方归属](NOTICE)
