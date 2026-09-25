"""Keep pytest temporary files local without reusing locked Windows paths."""

from pathlib import Path
import gc
import shutil
from uuid import uuid4

import pytest


@pytest.fixture(autouse=True)
def isolated_turn_router_backend(monkeypatch):
    # App tests replace SemanticTurnRouter with deterministic responses. An
    # earlier test may load the developer's .env; never let its live provider
    # selection change those test doubles. Backend tests override explicitly.
    monkeypatch.setenv("EGOASSIST_TURN_ROUTER_BACKEND", "qwen")


def pytest_configure(config):
    if config.option.basetemp is None:
        runs = Path(__file__).parents[1] / ".test-runs"
        runs.mkdir(exist_ok=True)
        generated = runs / uuid4().hex
        config.option.basetemp = str(generated)
        config._egoassist_generated_basetemp = generated


def pytest_sessionfinish(session, exitstatus):
    # Preserve failed runs and explicit --basetemp paths for debugging.
    generated = getattr(session.config, '_egoassist_generated_basetemp', None)
    if exitstatus == 0 and generated is not None:
        try:
            # Collect closed application cycles before removing SQLite files on Windows.
            gc.collect()
            shutil.rmtree(generated)
        except FileNotFoundError:
            pass
        except OSError as exc:
            reporter = session.config.pluginmanager.get_plugin('terminalreporter')
            if reporter:
                reporter.write_line('Test temporary files remain at ' + str(generated) + ': ' + str(exc))
