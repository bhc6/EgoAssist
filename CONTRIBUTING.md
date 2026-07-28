# Contributing

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
