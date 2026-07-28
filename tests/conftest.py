"""Keep pytest temporary files local without reusing locked Windows paths."""

from pathlib import Path
from uuid import uuid4


def pytest_configure(config):
    if config.option.basetemp is None:
        runs = Path(__file__).parents[1] / ".test-runs"
        runs.mkdir(exist_ok=True)
        config.option.basetemp = str(runs / uuid4().hex)
