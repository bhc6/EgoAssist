import json
from pathlib import Path

from egoassist import BUILTIN_SKILLS_ROOT, CONSOLE_ROOT


ROOT = Path(__file__).parents[1]


def test_domain_examples_do_not_leak_into_framework_source():
    forbidden = ("做菜", "做饭", "烹饪", "厨房", "番茄炒蛋", "组装家具", "安装净水器")
    framework_files = [
        path for path in (ROOT / "egoassist").rglob("*")
        if path.is_file() and "skills" not in path.parts and path.suffix in {".py", ".js", ".html", ".css"}
    ]
    leaks = {
        str(path.relative_to(ROOT)): term
        for path in framework_files
        for term in forbidden
        if term in path.read_text(encoding="utf-8")
    }
    assert leaks == {}


def test_bundled_tool_names_exist_only_in_skill_contracts():
    tool_names = set()
    for manifest_path in BUILTIN_SKILLS_ROOT.glob("*/skill.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tool_names.update(tool["name"] for tool in manifest.get("tools", []))
    core = (ROOT / "egoassist" / "realtime" / "tools.py").read_text(encoding="utf-8")
    frontend = (CONSOLE_ROOT / "app.js").read_text(encoding="utf-8")

    assert tool_names
    assert all(name not in core for name in tool_names)
    assert all(name not in frontend for name in tool_names)
