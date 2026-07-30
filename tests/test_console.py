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
    assert '"?session=" + encodeURIComponent(selectedSessionId)' in script
    assert 'id="mirrorCamera"' in html
    assert 'vendor/livekit-client.umd.js?v=2.21.0' in html
    assert 'app.js?v=20260730-m4' in html
    assert '/api/livekit/token' in script
    assert 'activateWebSocketMediaFallback' in script
    assert 'interactionTransport === "livekit"' in script
    assert 'context.translate(canvas.width, 0)' in script
    assert 'coordinateConvention: mirrorCamera ? "horizontally-mirrored" : "camera-native"' in script
    assert 'classList.toggle("mirrored", mirrorCamera)' in script
    assert "generation impact" not in script
    assert "current-turn injection" in script


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
    assert "{...payload, sessionId: selectedSessionId}" in script
    assert script.count("sessionId: selectedSessionId") >= 5
    assert "let connectionGeneration = 0" in script
    assert "generation !== connectionGeneration" in script
    assert "const connectionSessionId = selectedSessionId" in script
    assert "let mediaPromise = null" in script
    assert "let mediaGeneration = 0" in script
    assert "generation !== mediaGeneration" in script
    assert "acquired.getTracks().forEach(track => track.stop())" in script
    assert "socket || connectingPromise ? disconnectRealtime()" in script


def test_frontend_exposes_operable_work_center_and_capability_truth():
    html = (CONSOLE_ROOT / "index.html").read_text(encoding="utf-8")
    script = (CONSOLE_ROOT / "app.js").read_text(encoding="utf-8")

    for identifier in ("capabilityTruth", "intentList", "jobList", "resultInbox"):
        assert f'id="{identifier}"' in html
    assert "state.workLedger" in script
    assert "state.capabilityCatalog?.platform" in script
    assert 'post("/api/intents/focus"' in script
    assert 'post("/api/jobs/cancel"' in script
    assert 'post("/api/results/dismiss"' in script
    assert "availability.executable" in script
    assert 'id="reminderForm"' in html
    assert 'post("/api/reminders"' in script
    assert 'item.id === "timer-delivery"' in script


def test_frontend_renders_task_artifacts_and_consent_gated_human_help():
    html = (CONSOLE_ROOT / "index.html").read_text(encoding="utf-8")
    script = (CONSOLE_ROOT / "app.js").read_text(encoding="utf-8")

    for identifier in (
        "artifactList", "artifactDialog", "artifactCanvas", "artifactPrevious",
        "artifactNext", "artifactZoomIn", "artifactZoomOut", "humanHelpDialog",
        "humanHelpPreview", "humanHelpConsent",
    ):
        assert f'id="{identifier}"' in html
    assert 'import("./vendor/pdf.mjs?v=6.2.108")' in script
    assert 'pdfjs.GlobalWorkerOptions.workerSrc' in script
    assert 'post("/api/human-help/preview"' in script
    assert 'post("/api/human-help/submit"' in script
    assert 'accepted: true' in script
    assert "preview.destination" in script
