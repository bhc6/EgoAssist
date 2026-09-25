# 对话记录的即时显示与最新位置

日期：2026-09-25

## 本次问题与核对

用户反馈刚发送的“你好”没有出现在对话记录。核对当前会话原始日志与 `conversation_page`，该条文字输入和助手回复都已持久保存，并位于最新消息末尾；请求日志中的对话接口持续返回成功。本例没有发现原文丢失，不能将列表未见直接解释为记忆写入失败。

旧界面尊重列表滚动位置，但没有新消息提示或回到最新的入口；停留在历史位置时，新消息追加在列表底部，不一定可见。列表在其他工作区隐藏时，滚动高度为零，加载后返回也可能失去自动跟随。另发现语音即时显示缺口：provider 的 `user_transcript` 没有 `turnId`，而历史面板仅接收带该编号的实时输入，因此语音需等 HTTP 轮询才能补入。

## 修改

- 新增“回到最新”和未读新消息数量。用户阅读旧记录时不强行拉回；主动发送文字时返回最新对话。
- 保存明确的跟随状态，首次打开、切换会话以及隐藏工作区返回时都能定位到最新消息。
- 语音识别完成后先由原有 `TurnCoordinator`/`CaseWorkLedger` 保存输入，再发布携带真实 `turnId`、时间戳和 `persisted` 标记的事件，前端立即显示。同一句话连续说两次保持两条不同轮次，后续历史轮询按编号去重。
- 处理队列、记忆写入、语音身份判定和回答调度保留原语义；“对话记录”仍展示当前会话，不合并其他会话。

## 验证

`tests/test_conversation_history.py` 验证发布前已保存、重复措辞不被合并、重启可读；已有测试覆盖长记录分页、删除缓存失效和账号隔离。

`scripts/verify_conversation_history_browser.py` 验证 128 条历史、55 条连续新增、完整长回答、打断片段、实时与持久去重、切换会话、阅读位置、新消息提示、文字发送返回最新及隐藏工作区恢复。首次追加隐藏工作区测试时，脚本将仅修改 hash 的导航误当整页刷新，导致期望条数错误；已显式 reload，保留该失败报告供审计。

`scripts/verify_conversation_live_real.py` 使用真实 TTS→ASR→对话服务和浏览器；临时将 HTTP 历史响应固定为空，确认语音依然凭 socket 回执即时出现，再恢复 HTTP 以检查去重。还验证文字“你好”、回答、刷新保留和桌面/手机布局。合成语音不是物理麦克风测试。

本轮完整回归 **859 项通过，0 失败、0 跳过**，有 3 条现有 aiohttp 弃用提示。隔离服务与正式 **8770** 服务的真实验收均通过，页面脚本无异常；正式服务返回的历史脚本与本地文件一致。验证对象是记录的完整性、可见性及身份关联，不将模型生成回复等同于回答质量保证。

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe scripts/verify_conversation_history_browser.py --output outputs/history-browser
.\.venv\Scripts\python.exe scripts/verify_conversation_live_real.py --output outputs/history-real
.\.venv\Scripts\python.exe scripts/verify_conversation_live_real.py --base-url http://127.0.0.1:8770 --output outputs/history-production
```

本地验收产物位于 `outputs/conversation-display-20260925-*`、`outputs/conversation-live-real-20260925-*`、`outputs/conversation-live-production-20260925-r1/` 和 `outputs/conversation-history-regression-20260925.xml`。产物不提交 Git；用户原始日志只读核对，不搬入测试账号或代码库。
