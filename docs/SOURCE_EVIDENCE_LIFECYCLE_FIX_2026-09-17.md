# 证据生命周期修复与实验口径校正

2026-09-17。本轮保持检索排序、模型和独立日记实现不变，修复已复现的基础问题。

## 删除与异步写入

历史 `evidence.package` 现在拥有带 `evidence-snapshot:` 前缀的依赖标识。原文删除会沿该标识继续清除旧版请求、响应和失败日志；依赖图在账户会话 journal 内建立。旧版 `answer.request` 的序列化上下文依赖从结构化 `answer.job.context` 补齐，不解析任意提示词字符串。

新原生回答的证据包、请求、响应和失败日志保存明确的来源或前序事件 ID。`SessionJournal.append(required_event_ids=...)` 在同一进程锁及文件锁内刷新日志、检查依赖并写入，拒绝使用缺失或已删除的事件。依赖在等待模型或重试期间删除时，迟到内容和错误不再重新持久化。日志写入不创建已经消失的账户目录。

后台回答也检查请求与任务依赖；即使已有 `preparedResult`，发布前仍验证证据快照。已删除任务不会被异常处理重新建立。

这些保证覆盖本次原生历史回答及后台回答审计路径，不表示已经完成全部外部模型服务的数据删除，也不表示所有跨模块派生文档生命周期均已统一。

## 快照范围

新建或追加无关会话不再全量删除快照。会话替换、来源删除或会话消失时，仅复查受影响会话中快照的来源 ID 与哈希，并删除失效快照。删除同会话中未进入证据包的来源也会保留有效快照。显式账户维护仍允许丢弃整个可重建索引。

## 校正后的开发结果

原始 r1/r2 输出不修改。重算直接读取冻结的导入收据与 304 个证据包，验证问题对应关系及数据集 SHA256，没有重新检索，也没有新增质量评测调用。

| 可回答题指标 | 结果 |
| --- | --- |
| 题数 | 233，其中 231 题有标准引用 |
| 候选证据召回 | 275/309，89.00% |
| 最终证据召回 | 225/309，72.82% |
| 标准证据齐全 | 171/231 道有引用题 |
| 多跳题最终证据召回 | 55/106，51.89% |
| 多跳题标准证据齐全 | 11/43，25.58% |
| 最终缺失引用 | 84 个：67 个在候选内，17 个不在候选也未被相邻展开补回 |

类别 5 单独统计为对抗题上下文覆盖，不能当作可回答题成功。复合标注 `D8:6; D9:17` 拆为两个引用。旧报告 `passed=true` 只代表少量 HTTP 调用成功，不能表示架构达到质量目标。

脚本现在分别返回 `operationalChecksPassed`、`finalEvidenceRecallTargetMet`、`answerQualityEvaluated` 和 `qualityTargetsMet`。当前召回目标明确未达标；答案准确率、全量错误拒答率仍未知。达到召回目标也不会自动把未评估的答案质量判为通过。

原生回答新增 `validation` 字段：结构和引用 ID 已通过校验，`entailment=not-verified`，`statusAuthority=model-declared`。这明确标注验证边界，没有解决自由回答与 claims 的语义一致性问题。

## 验证证据

- 完整测试：691 passed，3 条已有弃用提示，64.19 秒。覆盖独立日记、原协议、删除传播、成功/失败响应期间删除、账户删除后不重建文件、无关会话快照、后台已生成结果发布及评测口径。
- [真实 HTTP 与模型验证](../outputs/source-evidence-lifecycle-fix-20260917/real-smoke-r2/acceptance.json)：导入取件码，建立快照，新建另一会话，通过旧快照回答，删除来源，核对审计清除及另一会话快照可用。保存提供商请求 ID 与 token usage；被删除原文及响应不另外复制留存。
- [原实验校正报告](../outputs/source-evidence-lifecycle-fix-20260917/recount-final.json)：逐题命中、分题型汇总、原始输入哈希及明确未达标状态。
- 初次真实验证脚本错误地要求创建会话返回 200，实际接口按契约返回 201；模型尚未调用。修正脚本后复测通过，失败记录保留在 `real-smoke/acceptance.json`。

```powershell
.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider
.\.venv\Scripts\python.exe scripts/recount_source_evidence.py --input outputs/source-evidence-20260917-r2 --output outputs/new-recount.json
.\.venv\Scripts\python.exe scripts/verify_source_lifecycle_real.py --output outputs/new-lifecycle-smoke
```

下一轮应针对候选进入最终证据时的损失做排序和多跳取证消融，并继续单列原生协议、旧 WebSocket 协议与离线标准证据诊断。当前没有达到最终证据召回 80% 或错误拒答率 10% 以下的验收目标。
