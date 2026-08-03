# Contributing

## Architecture Overview

EgoAssist is structured in two layers:

**Official foundation (realtime-ready / benchmark-testable)**
- `HierarchicalMemory` — in-process visual memory, benchmark localization (Pipeline 1)
- `TimelineMemory` — SQLite WAL persistent memory, benchmark QA evaluation (Pipeline 2)
- LiveKit — realtime media transport
- Temporal — durable workflow execution

**Self-developed memory layer (task-aware, review complete)**
- `RealtimeEpisodeMemory` — the single memory object used in the realtime path (`run_egoassist.py`). It unifies BM25 episodic retrieval with task-preference operations (`propose` → `confirm` cycle) via internal lazy delegation to `PreferenceMemory`. All seven task-preference methods are exposed on this object directly.

The benchmark path (`TimelineAssistant`) accesses `TimelineMemory` directly and does not go through `RealtimeEpisodeMemory`.

## Development setup

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
python -m pytest
```

Keep changes focused and add tests for behavioral changes. Real data, model weights, API keys,
generated outputs, and local caches must not be committed.

## Pull requests

- Explain the problem and the behavioral change.
- State which backend and dataset scope were tested.
- Separate Mock results from real-model results.
- Report failures and abstentions; do not remove unfavorable samples.
- Use video-level calibration/test splits for real selective-risk claims.

## Data and models

Contributors are responsible for complying with dataset and model licenses. Add download
instructions or metadata, not redistributed assets, unless redistribution is explicitly allowed.
