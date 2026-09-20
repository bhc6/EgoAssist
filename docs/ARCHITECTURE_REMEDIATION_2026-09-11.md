# 架构整改与验证

本轮处理数据生命周期、执行模块归属、共享底座和长历史成本。运行形态仍是本地单进程；未引入微服务或额外数据库。

## 模块归属

| 模块 | 责任 |
| --- | --- |
| `run_egoassist.py` | 命令行、环境加载和启动；保留脚本使用的兼容导出 |
| `application/composition.py` | 构造服务、案例恢复、路由注册与生命周期连接 |
| `application/*_api.py` | 认证后的 HTTP 输入校验、服务调用和响应 |
| `application/realtime_connection.py` | WebSocket/LiveKit 连接、媒体收发与回调装配 |
| `realtime/turn_coordinator.py` | 语义准备、顺序提交、打断与任务执行 |
| `realtime/interaction_service.py` | Skill/任务变更、交互结果和记忆调用 |
| `realtime/observation_service.py` | 采样上下文、观察校验与结果归属 |
| `memory/commit.py` | 记忆写入政策、草稿、同意与来源 |
| `core/` | 共享契约、配置、日志、增量投影及删除依赖规则 |
| `application/retention.py` | 暂停账户写入、清理与恢复 |

核心包不能向上依赖应用、实时、产品或记忆包，`test_architecture_boundaries.py` 检查此规则。旧契约、日志与配置导入路径保留兼容导出。

## 数据生命周期

会话日志保存来源，Timeline 保存记忆，产品数据库保存派生投影。保留策略先把过期事件的内容原子替换为仅含事件 ID、时间和失效标识的墓碑，沿来源依赖清理派生结果和记忆，再清理无剩余引用的媒体。摘要引用多个来源时，摘要失效不反向删除仍有效的其他来源。诊断日志按过期时间和被删除来源逐行过滤。

原子替换使已打开的日志实例和增量游标失效。产品投影还会检查过期策略，不能从旧日志重新导入过期文本。启动时先执行保留策略，再启动作业恢复；后台默认每小时执行一次，也可立即应用。

清理时结束目标账户连接、停止在途写入和关联作业、清空案例缓存，之后恢复仍有效的作业。其他账户不在清理范围内。被删除来源所支持的报告或答案可能无法继续，不能假装其证据仍有效。环境观察实际保存采样图片和引用背景，隐私说明已与实现同步。`make clean` 不再包含用户数据目录。

## 历史成本

日志提供按类型/尾部读取、可失效增量游标和共享增量投影。作业、工作账本、三链路状态、上下文和数据库投影消费新增事件；状态输出保留有限窗口。原始日志仍完整保留到保留策略生效，初次恢复、导出和清理仍需要处理相应历史。

检索默认最多对 256 个候选做 Python 评分，候选来自精确键、FTS、日期和近期数据；审计返回 `totalCandidates`、`candidateLimit`、`scannedCards` 和 `candidatesTruncated`。截断限制成本，不保证所有同义改写都能召回。索引查询和候选总量统计仍受数据库规模影响。

本机一次隔离基准：20,000 条日志、1,000 张记忆卡。三链路状态投影中位 0.297 ms，数据库增量投影 0.451 ms，160 条日志尾部读取 0.783 ms，记忆检索 9.195 ms；检索评分 256 张卡，统计总数 1,000。数据来自合成记录，不是完整 HTTP 端到端、模型或眼镜硬件延迟。

## 验证方式

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider
.venv\Scripts\python.exe scripts/benchmark_local_scaling.py
.venv\Scripts\python.exe scripts/verify_browser.py --output outputs/new-architecture-browser
```

新增回归覆盖过期删除后重启、原始日志及关联记忆/媒体、全局记忆策略、HTTP 缓存失效、共享摘要的来源保护、增量游标重置、候选上限和核心依赖方向。GitHub Actions 运行 Windows/Linux Python 3.13 回归。

最终本地全量结果：490 项通过，耗时 38.76 秒；保留 3 条原有 aiohttp WebSocket 超时参数弃用警告。新增取消/失败路径缓存失效、实际删除数量及重复清理计数回归。Ruff F 类检查和 `git diff --check` 通过。CI 配置已加入，本轮未验证远端 CI 运行结果。

本轮本地 Edge 验证通过：注册、案例、两类 Skill、登录恢复和待发送文本恢复；1440/390 像素无横向溢出，无页面错误与失败请求。wheel 构建通过。真实供应商推理与眼镜硬件未在本轮重新验收。

## 核心精简

后续清理删除了根目录重复观察清单、重复 GPU 依赖列表、绑定本机镜像哈希的 Docker 评测脚本、两个无调用私有方法和 20 项无用导入。测试与 Docker 统一使用包内观察资源；保留通用离线评测、真实验收脚本、兼容接口及历史失败证据。

删除未使用的 `livekit-agents` 安装依赖，保留实际使用的 LiveKit RTC/API。旧 `requirements-lock.txt` 只是历史环境快照，移至 `docs/validation/requirements-windows-py313-historical.txt`，不再作为当前安装入口。依赖以 `pyproject.toml` 为准；已有虚拟环境未执行卸载。

README 删除重复的日期进度与历史性能叙述，突出三链路、启动和边界；贡献指南同步当前架构。Docker 构建上下文排除虚拟环境、缓存、密钥、日志及所有本地演示数据目录。

精简后全量 493 项通过，3 条原有依赖弃用警告；源码及脚本 Ruff F 检查通过。Edge 在 1440/390 像素下验证通过，未调用真实云端回答。wheel 构建通过，新增测试禁止主应用导入 `livekit.agents`，并保护资源唯一来源和 Docker 排除规则。旧 `.test-runs` 的部分目录存在访问权限限制，未修改权限或强行删除；用户数据、模型与实验输出未删除。
