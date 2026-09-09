# Skill 生命周期检查与真实验收

## 发现与修复

1. 控制台退出 / 重置取消了内存中的分类任务，但未把持久化 turn inbox 标为 cancelled。重连会恢复旧话语，再次激活 Skill。新增重连测试实际复现退出后 `show_reference` 重新出现；修复后两种入口均通过。取消记录保留原始话语和原因，普通网络断线仍保留恢复机制。
2. 非会话 API 重复激活同一 Skill 会清空已选任务，会话 API 则保持不变。统一为保持当前任务，并将 Skill 名称校验提前到连接失效之前。

取消逻辑位于 `egoassist/realtime/work_ledger.py`，生命周期协调仍在 `run_egoassist.py`；未增加另一套状态机。

## Skill 本身的结论

两个内置 Skill 的指令及声明的 Markdown 资源能够加载；任务状态由运行时维护，不能把模型说“完成了”当成后台完成。自动激活需要语义意图和 manifest 证据；没有证据的错误模型提议已由回归验证不会改变当前 Skill、任务和工具。

退出后，活动 Skill、任务、资料授权与领域上下文均清除；桥接层发送空工具列表。暂停保留进度，断线保留可恢复状态，明确退出才卸载领域。这次没有增加“沉默一段时间自动退出”或“完成最后一步自动卸载”的策略。

Skill 是领域指令、资源和任务定义，不是能独立运行所有功能的程序。未配置 Temporal 时计时器仍不可执行，人工求助取决于连接器。视觉安全策略是指令边界，不等于经过验证的现实安全识别能力。

## 真实 Qwen 结果

模型：`qwen3.5-omni-flash-realtime`；路由：`qwen-flash`。生产 HTTP / WebSocket 处理路径，无 mock。输入为测试文本控制，输出包含真实供应商音频、完整转写与未中断的 `response.done`。

成功运行：`outputs/skill-lifecycle-20260909-2`，会话 `69e9167c47694352`。

- 11 个回合全部通过：番茄炒蛋启动、确认第一步、暂停、重启服务后恢复、退出做菜模式、提及菜名但不启动、普通取物说明、重新从头启动、切换组装凳子、退出 Skill、重启后的普通问候。
- 两次应用关闭后重新创建，分别验证暂停进度和已退出状态；另验证 API 激活 / 退出后的 WebSocket 重连。
- 路由耗时 248.1–461.3 ms；中位数 396.4 ms。本轮 11 次分类均成功，无路由超时。
- 首音频 919.6–1350.9 ms；中位数 1233.6 ms。自然语言退出首音频分别为 1257.8、1261.7 ms。
- 恢复的任务位于 wash 且 paused=true；退出再激活位于 prepare。普通字数问题正确回答四个字，没有加载做菜 Skill。

首次尝试因验收脚本将资料字段 `artifact_id` 误写成 `id` 失败，报告保留为 run-1；它不是产品通过证据。历史其他验收仍有供应商超时记录，本次通过不代表以后零超时。

已归档 [成功报告](validation/2026-09-09-skill-lifecycle/run-2.json)、[筛选日志](validation/2026-09-09-skill-lifecycle/run-2.events.jsonl) 和 [首次失败](validation/2026-09-09-skill-lifecycle/run-1.json)。归档保留路由、响应终止、连接、恢复和状态检查点事件；检查点省略重复任务定义，仅保留步骤状态。完整隔离运行日志保留在 outputs 中，未提交数据库、密钥或音频。

## 回归与运行

全量回归：354 passed，4 个已有 aiohttp 警告，耗时 51.86 秒；JUnit 位于 `outputs/skill-lifecycle-tests.xml`。

```powershell
.venv\Scripts\python.exe -m pytest -o cache_dir=outputs/pytest-cache-lifecycle
.venv\Scripts\python.exe scripts/verify_real_skill_lifecycle.py --output outputs/new-skill-lifecycle-run
```

修复后的常驻服务已运行在 http://127.0.0.1:8780，沿用 `.egoassist-real-demo` 数据。真实服务检查会话 `c93213a48ea74fab` 已连接 Qwen、启动 assembly-coach、收到 48 个音频包和完整助手转写。日志位于 `outputs/demo-lifecycle-8780.*.log`。

延迟不包含麦克风采集、ASR、用户说话时长和真实扬声器播放。本次没有测试真实眼镜硬件、持续视觉、日报 / 周报 / 月报，也没有把文本声明当作物理操作证据。
