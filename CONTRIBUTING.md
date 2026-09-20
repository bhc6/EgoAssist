# Contributing

## Core Architecture

Observation, interaction, and memory are the runtime lanes. See
[ARCHITECTURE.md](ARCHITECTURE.md) and [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md).

- `application/` owns composition, HTTP/media adapters, and account/case lifecycle.
- `realtime/` owns observation and turn execution; `memory/` owns memory policy and storage.
- `core/` owns shared contracts, journals, and projections without upward imports.
- `product/` supplies jobs, privacy, artifacts, and external connectors.
- Offline capture/retrieval/evaluation modules are research tools, not a second runtime.

Keep `run_egoassist.py` limited to startup and compatibility exports. Put domain
policy in Skills/prompts, validate permissions and time in code, and persist state
with explicit ownership. Model output is a proposal, not verified evidence.

## Development

Create and activate a virtual environment, then run:

```bash
python -m pip install -e ".[dev]"
python -m pytest -p no:cacheprovider
```

`pyproject.toml` is the dependency source of truth. Offline model work uses
`.[gpu,api]`, with platform-specific PyTorch installed separately. Historical
environment snapshots under `docs/validation/` are evidence, not install guides.

Test cancellation, retries, restart, ownership, and source deletion when affected.
Do not remove compatibility imports solely because internal callers no longer use
them. Resources belong under `egoassist/`; tests must use those packaged resources.

## Review And Evidence

- Explain the behavior change and which backend/dataset scope was tested.
- Separate deterministic substitutes from real-model results.
- Preserve failures and abstentions; do not remove unfavorable samples.
- Use video-level calibration/test splits for selective-risk claims.
- Keep dated experiments in the documentation index, not in the main README.
- Never commit user data, credentials, weights, generated outputs, or local caches.

Contributors are responsible for dataset and model licenses. Add download
instructions or metadata rather than redistributing assets without permission.
