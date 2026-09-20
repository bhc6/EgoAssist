> VALIDITY CORRECTION (2026-09-12): Historical exploratory results only; these samples were used in tuning. Legacy audit/expansion pools did not enforce common hard gates and hybrid scores were not pure RRF. These recall values do not establish safe retrieval or independent generalization. See [revised protocol](EGOLIFE_EXPERIMENT_PROTOCOL_2026-09-12.md).

# EgoLife R10：真实语义路线接入

回测 runner 现已同时执行 Timeline、hashed-vector、Hybrid 和 `SemanticEmbeddingAdapter`。真实语义路线依赖 `sentence-transformers`，本环境未安装，因此结果明确为 `status=unavailable`，source recall 等质量指标为 `null`，不会被误计为零分或伪造结论。结果保存在 `artifacts/backtest-r10/backtest.json`。安装依赖后无需改代码即可运行同一协议和 manifest。
