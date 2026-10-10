"""Shared test helpers.

These were copy-pasted across many test modules (identical `register_client`
bodies in four files, the same `create_app(...)` call in dozens of places).
They live here so a change to the app's startup contract updates one place.
This module must not import from test modules, to keep the import graph acyclic.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from egoassist import BUILTIN_SKILLS_ROOT

ROOT = Path(__file__).parents[1]

TEST_PASSWORD = "baseline-pass-123"
DEFAULT_MODEL = "test-realtime"
DEFAULT_VOICE = "Ethan"


async def register_client(client, username="researcher"):
    """Register a user and return the user record."""
    response = await client.post(
        "/api/auth/register", json={"username": username, "password": TEST_PASSWORD}
    )
    assert response.status == 200
    return (await response.json())["user"]


def test_app(tmp_path, *, model=DEFAULT_MODEL, voice=DEFAULT_VOICE, memory_path=None):
    """Build the app the way the test suite always builds it.

    Imported lazily so tests that only need the helpers do not pay for a full
    application import, and so the module stays importable without the app.
    """
    from run_egoassist import create_app

    return create_app(
        str(BUILTIN_SKILLS_ROOT), None,
        str(memory_path or (tmp_path / "memory.json")), model, voice,
    )


def node_binary():
    """Return the node executable, or None when Node is unavailable."""
    return shutil.which("node")


def require_node():
    node = node_binary()
    if not node:
        pytest.skip("Node is required for console tests")
    return node


def run_node(program, *, via_stdin=True, timeout=15):
    """Run a JS snippet with node, failing the test on a non-zero exit."""
    node = require_node()
    if via_stdin:
        return subprocess.run([node, "-"], input=program, text=True,
                              encoding="utf-8", check=True, timeout=timeout)
    return subprocess.run([node, "-e", program], capture_output=True, text=True,
                          encoding="utf-8", check=True, timeout=timeout)


def temporal_test_server_available():
    """True when the Temporal CLI test binary has already been downloaded.

    ``WorkflowEnvironment.start_time_skipping`` downloads it on first use; when
    that download cannot happen (offline CI, no cache) it raises RuntimeError
    instead of skipping, so tests need to check first.
    """
    cache = ROOT / ".cache" / "temporal-test-server"
    if not cache.is_dir():
        return False
    return any(path.name.startswith("temporal-test-server") for path in cache.iterdir())


requires_temporal = pytest.mark.skipif(
    not temporal_test_server_available(),
    reason="Temporal test server binary not downloaded (set it up once while online)",
)
