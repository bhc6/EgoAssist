from html.parser import HTMLParser
from pathlib import Path

from egoassist import CONSOLE_ROOT


ROOT = Path(__file__).parents[1]


class _ButtonParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.buttons = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "button" and values.get("id"):
            self.buttons[values["id"]] = values


def test_task_dialog_cancel_bypasses_required_field_validation():
    parser = _ButtonParser()
    parser.feed((CONSOLE_ROOT / "index.html").read_text(encoding="utf-8"))

    assert parser.buttons["cancelTask"]["type"] == "button"
    assert parser.buttons["createTask"]["type"] == "submit"

    script = (CONSOLE_ROOT / "app.js").read_text(encoding="utf-8")
    assert '$("#cancelTask").onclick = () => $("#taskDialog").close();' in script


def test_frontend_exposes_memory_pipeline_and_treats_steps_as_reference_map():
    html = (CONSOLE_ROOT / "index.html").read_text(encoding="utf-8")
    script = (CONSOLE_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="memoryFlow"' in html
    assert 'id="assistantCaption"' in html
    assert 'id="memoryScope"' in html
    assert 'id="memoryProfile"' in html
    assert 'id="preferenceEnabled"' in html
    assert 'id="episodicEnabled"' in html
    assert 'id="mapStatus"' in html
    assert 'id="progress"' not in html
    assert "task.steps.filter" not in script
    assert "可选证据锚点" in script
    assert "助手语音字幕" in script
    assert "/api/sessions/memory-policy" in script
    assert "generation impact" not in script
    assert "context injection" in script


def test_frontend_prioritizes_pipeline_and_event_provenance():
    html = (CONSOLE_ROOT / "index.html").read_text(encoding="utf-8")
    script = (CONSOLE_ROOT / "app.js").read_text(encoding="utf-8")
    assert 'id="pipeline"' in html
    assert 'id="eventInspector"' in html
    assert 'id="timeline"' in html
    assert 'id="promptContext"' in html
    assert "parent ids" in script
    assert "static-skill-task-injection-v1" not in html


def test_frontend_supports_unloaded_runtime_and_audio_attribution():
    html = (CONSOLE_ROOT / "index.html").read_text(encoding="utf-8")
    script = (CONSOLE_ROOT / "app.js").read_text(encoding="utf-8")
    assert 'id="intentRouting"' in html
    assert 'id="audioAttribution"' in html
    assert 'id="episodicEpisodes"' in html
    assert "state.activeSkill?.name || \"\"" in script
    assert "task?.decision || state.genericDecision" in script
    assert 'type: "media_config"' in script


def test_frontend_tool_invocation_is_manifest_driven():
    script = (CONSOLE_ROOT / "app.js").read_text(encoding="utf-8")
    assert "defaultToolArguments(declaration)" in script
    assert "declaration.arguments?.properties" in script
    assert 'tool === "set_timer"' not in script
    assert "state.activeSkill?.allowCustomTasks" in script
