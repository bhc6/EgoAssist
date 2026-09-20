> VALIDITY CORRECTION (2026-09-12): Historical exploratory results only; these samples were used in tuning. Legacy audit/expansion pools did not enforce common hard gates and hybrid scores were not pure RRF. These recall values do not establish safe retrieval or independent generalization. See [revised protocol](EGOLIFE_EXPERIMENT_PROTOCOL_2026-09-12.md).

# EgoLife Qwen Embedding 完整回测

使用真实 DashScope `text-embedding-v3`，递归 EgoLife 数据，110 个可评估问题，3 个 seed。Qwen source recall 均值 **0.7909**，citation validity 为 1.0，检索中位延迟约 **322 ms**。Timeline 为 0.2182，Hybrid 为 0.2273；Qwen 语义召回显著提升，但延迟高于本地路线，暂不直接进入前台热路径。应采用后台 embedding、缓存和批处理，前台保留 Timeline 硬约束与快速 fallback。

结果：`artifacts/backtest-qwen-full-r2/backtest.json`。
