import ast
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).parents[1]


def test_core_has_no_application_lane_or_product_dependencies():
    forbidden = (
        "egoassist.application",
        "egoassist.realtime",
        "egoassist.memory",
        "egoassist.product",
        "run_egoassist",
    )
    for path in (ROOT / "egoassist" / "core").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(name.startswith(forbidden) for name in names), path


def test_entry_is_only_launch_and_services_are_not_http_handlers():
    entry = ast.parse((ROOT / "run_egoassist.py").read_text(encoding="utf-8"))
    assert {node.name for node in entry.body if isinstance(node, ast.FunctionDef)} == {
        "runtime_root",
        "main",
    }
    for name in (
        "egoassist/memory/commit.py",
        "egoassist/realtime/interaction_service.py",
    ):
        source = (ROOT / name).read_text(encoding="utf-8")
        assert "run_egoassist" not in source
        assert "request.json" not in source
        assert "web.json_response" not in source


def test_clean_does_not_target_runtime_data():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    clean = makefile.split("clean:", 1)[1]
    assert ".egoassist" not in clean
    assert "data_real" not in clean
    assert "outputs" not in clean


def test_observation_resources_have_one_packaged_source():
    import json
    from egoassist import PACKAGE_ROOT

    assert not (ROOT / "watchlists" / "medication.json").exists()
    for name in ("medication", "open"):
        resource = PACKAGE_ROOT / "watchlists" / f"{name}.json"
        assert json.loads(resource.read_text(encoding="utf-8"))["id"]
    assert "COPY watchlists" not in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_application_does_not_require_livekit_agents(tmp_path):
    script = """
import importlib.abc
import sys
class NoAgents(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'livekit.agents' or fullname.startswith('livekit.agents.'):
            raise AssertionError('Unused agents framework imported: ' + fullname)
sys.meta_path.insert(0, NoAgents())
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.application.composition import create_app
app = create_app(str(BUILTIN_SKILLS_ROOT), None, sys.argv[1], 'test', 'Ethan')
assert list(app.router.routes())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "runtime.json")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"livekit-agents' not in (ROOT / "pyproject.toml").read_text(
        encoding="utf-8"
    )


def test_docker_context_excludes_local_data_and_environments():
    patterns = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert {
        ".venv/",
        ".egoassist*/",
        ".env",
        ".env.*",
        "*.key",
        ".cache/",
        "outputs/",
    } <= patterns
