# 工作区整理记录

2026-09-17。整理目标是让当前实现、推荐入口和历史证据有明确归属，并删除可确认无用或可重建的内容。

## 已删除

- 59 个旧测试临时目录。它们由 `tests/conftest.py` 每次测试生成随机 ID，原先没有成功后回收机制；保留最新三个已有测试目录。
- 21 个项目源码、脚本和测试的 Python 字节码缓存目录。
- 5 个旧 wheel 构建目录：architecture-review-wheel、architecture-review-wheel-final、core-cleanup-wheel、interaction-v16-wheel、skill-catalog-wheel。
- `egoassist/realtime/settings.py`、`egoassist/realtime/tool_activity.py` 两个转发兼容文件。实际实现已在 `egoassist/core/`，内部调用和测试改为直接使用该唯一实现。

文件系统清理合计 **85 个目录、17,127 个文件、845,137,093 字节（805.99 MiB）**，不含上述两个源码文件。清单见[预检](../outputs/workspace-cleanup-20260917/plan.json)与[执行结果](../outputs/workspace-cleanup-20260917/deleted.json)。

有 61 个旧缓存目录因权限限制在预检阶段跳过，另一个 `outputs/answer-completion-wheel` 的安装包在删除时被系统拒绝，读取 ACL 也被拒绝。它们已记入清单，未声称全部清空，也未更改系统权限。

## 防止继续积累

成功的确定性测试会自动回收本轮生成的目录；失败、被中断的测试以及显式 `--basetemp` 继续保留供诊断。

Windows 清理入口为 `scripts/clean_workspace.ps1`，默认只预检，`-Apply` 执行。它使用固定候选范围，在删除前检查工作区绝对路径边界、链接、Git 跟踪文件及目录内容；默认保留最近三次测试目录，发现正在运行的本工作区 pytest 则停止。输出独立清单，不覆盖旧报告。

所有 `.egoassist*/` 运行目录统一从 Git 中排除，避免把演示账户、缓存和日志混入源码状态。安装依赖和模型权重不属于这次清理对象。

## 当前与历史

[主 README](../README.md)和[文档导航](README.md)只保留当前使用说明与推荐路径。旧评测、方案对比、录屏、历史实现和失败分析的导航集中到[历史索引](HISTORY.md)，文件本身保持原路径，避免破坏证据链接。

目前没有证据证明某个方案在所有场景最优，因此按功能选择：

| 功能 | 当前推荐 | 保留的其他实现及原因 |
| --- | --- | --- |
| 语音、摄像头、工具 | 默认 Omni 主控制台 | 模块化后端仍用于对照与可选部署 |
| 日、周、月回顾 | 独立个人日记 `/diary.html` | `/api/insights` 仍有当前建议、旧报告及持久任务调用，不能直接当死代码删除 |
| 历史原文问答 | 原文证据服务 `/api/history/*` | Timeline 仍承担实时记忆与结构化检索；新服务尚未替代全部能力 |

同一服务下日记与助手共用登录；8779 和 8780 是已有的两个独立演示数据环境，不是产品要求用户分别登录。此次不合并账户库或迁移日报数据。

原始对话、模型日志、真实失败实验、数据集、历史录屏及活跃运行数据保留。这些是回归和诊断依据，不能仅凭目录日期或名称中的 r1/r2 判断无用。取舍依据详见[证据架构](SOURCE_EVIDENCE_ARCHITECTURE_2026-09-17.md)与[校正结果](SOURCE_EVIDENCE_LIFECYCLE_FIX_2026-09-17.md)。

## 兼容性

仓库内已没有对被删除两个转发模块的导入。外部代码如果使用过这些未文档化路径，需要改为 `egoassist.core.settings` 或 `egoassist.core.tool_activity`；功能实现和环境变量语义保持不变。

最终回归 **693 项通过**，83.93 秒，3 条已有 aiohttp 弃用提示；见[JUnit 报告](../outputs/workspace-cleanup-20260917/regression-final.xml)。个人日记、原协议、实时交互及新证据服务均纳入完整测试。

第一次回归同样通过，但退出时仍有 SQLite 连接等待垃圾回收，导致 Windows 无法删除临时目录。增加回收前的循环对象释放后，完整复测成功，测试目录自动删除；第一次[报告](../outputs/workspace-cleanup-20260917/regression.xml)继续保留。

`http://127.0.0.1:8780/`、同服务的 `/diary.html` 及原 8779 日记页面均返回 HTTP 200；本轮未重启服务。当前文档与历史索引的本地链接检查通过。页面可达不等于新增了模型或硬件质量验收。
