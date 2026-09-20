> VALIDITY CORRECTION (2026-09-12): Historical exploratory results only; these samples were used in tuning. Legacy audit/expansion pools did not enforce common hard gates and hybrid scores were not pure RRF. These recall values do not establish safe retrieval or independent generalization. See [revised protocol](EGOLIFE_EXPERIMENT_PROTOCOL_2026-09-12.md).

# EgoLife R6 回测记录

本轮在相同递归 EgoLife 数据、60 个问题和 seed `0,1,2` 上并行运行 Timeline、确定性 hashed-vector 和 hybrid 路线。hashed-vector 仅作为可复现的本地向量基线，不代表真实 embedding 模型。

| 路线 | source recall 均值 | retrieval median (ms) | citation validity |
|---|---:|---:|---:|
| Timeline lexical | 0.2333 | 8.96 | 1.0 |
| Hashed vector | 0.0000 | 0.015 | 1.0 |
| Hybrid（先硬约束后向量重排） | 0.2333 | 9.14 | 1.0 |

进一步检查逐问题结果发现：Timeline 的 hard-gated candidate recall 为 0.85，但最终 source recall@5 仅 0.233，主要失败来自词法 top-k 截断与跨表达排序，而非来源缺失。Hybrid 改为对完整硬约束候选池重排后，单 seed source recall 从 0.233 提升到 0.25（R8），延迟约 9.29 ms。纯 hashed-vector 无法替代语义检索。Mem0、Graphiti、Letta 在本环境未安装，已按 fail-closed 记录，不伪造对照结果。下一轮应接入真实 embedding provider，在同一 adapter 协议下复测，并继续按视频分组 bootstrap。

结果文件：`artifacts/backtest-r6/backtest.json`。
