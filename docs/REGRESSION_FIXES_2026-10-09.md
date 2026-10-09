# 2026-10-09 回归修复与提交验证

本轮整理 EgoAssist 待提交的模型调用审计、轮次隔离、路由上下文和跨日事件改动，并修复全量回归检查发现的三个失败。产品源码与研究文档在本仓库提交；独立 MemoryMechanismLab、第三方 checkout 和本地实验产物不属于本轮提交。

## 三个失败及修复

- 记忆层的 `insights.py`、`long_events.py` 引用了 `realtime.model_audit`，违反既有依赖约束。将完整审计实现移至 `core/model_audit.py`，内部调用直接使用 core，原 realtime 路径保留兼容导出。架构边界测试继续执行原有约束。
- 文字上下文回放测试使用旧 `SimpleNamespace` 请求，缺少已经加入正式合同的 `context_items`。改用实际 `_ResponseRequest`，继续检查时间政策只注入一次、原话时间标记和回答指令；不在产品代码中放宽请求合同。
- 观察写入失败测试的夹具缺少实际观察入口提供的 `observation_id` 和 reviews 状态。补齐夹具后，继续注入记忆读写错误，验证原始来源保留、降级状态记录和澄清抑制。

`ARCHITECTURE.md` 同步当前默认 modular 后端与共享审计职责。历史日期实验保留当时结果，不以当前默认设置改写旧实验。

## 本次提交覆盖的已有改动

- 模型与 JEV 调用审计：按显式标识关联真实输入/输出，权限按账户和案例校验，前端按需读取；历史未记录上下文不补造，重内容不进入常规状态轮询。
- 轮次隔离与恢复：以持久日志顺序限定前序上下文，文字回答保留受理时的上下文快照，恢复已完成轮次序号，重连重置播放状态。
- 路由上下文：JEV 和结构化阶段共享输入通道及近期交互，说话人等待受总分类预算限制，保留超时与失败记录。
- 事件和日记：支持带来源的跨日进展及阶段证据入口，日记最终整理复核原文，并保留实际模型调用记录与取消状态。
- 研究资料：保留论文、基准计划和启动检查，明确新的机制运行器属于独立研究项目。

## 验证

在 Windows / Python 3.13 本地环境执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -o addopts=
.\.venv\Scripts\python.exe scripts/verify_browser.py --output outputs/github-sync-browser-20261009
git diff --check
```

- 全量确定性回归：**1,004 passed**，耗时 124.54 秒；另有 3 条 aiohttp WebSocket timeout 参数弃用警告。
- Edge 页面验收：桌面 1440px、手机 390px 通过；注册、案例、Skill 切换、日记共享登录、三链路与诊断入口、刷新恢复通过；页面错误及失败请求均为零。
- 待提交 Python/JSON 和 JavaScript 语法检查通过；core/realtime 兼容导出为同一实现，差异空白检查通过。

浏览器记录位于本地 `outputs/github-sync-browser-20261009/`，不进入 Git。本轮未新增真实模型调用，结果不证明云端模型语义质量、硬件能力或独立科研机制效果。
