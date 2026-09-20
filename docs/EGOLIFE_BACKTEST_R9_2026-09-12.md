> VALIDITY CORRECTION (2026-09-12): Historical exploratory results only; these samples were used in tuning. Legacy audit/expansion pools did not enforce common hard gates and hybrid scores were not pure RRF. These recall values do not establish safe retrieval or independent generalization. See [revised protocol](EGOLIFE_EXPERIMENT_PROTOCOL_2026-09-12.md).

# EgoLife R9：候选融合回测

Hybrid 采用 reciprocal-rank fusion，将 Timeline 词法候选顺序与 hashed-vector 相似度融合；时间、主体、来源约束仍在融合前执行。60 题、seed 0 结果：Timeline source recall 0.2333，Hybrid 0.25；Hybrid retrieval median 8.77 ms。相较 R8 质量持平、延迟略降，说明融合层稳定但受限于本地 hashed-vector 表示能力。下一轮应替换为真实 embedding，并保留该融合策略作为可解释 fallback。
