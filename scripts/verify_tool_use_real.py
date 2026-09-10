"""Real Qwen text tool API, realtime grounded speech and browser tool receipts."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from egoassist.realtime.context_harness import ContextHarness, SessionJournal
from egoassist.realtime.omni import OmniBrowserBridge
from egoassist.realtime.tools import SkillToolExecutor
from egoassist.realtime.tool_model import ToolModel
from run_egoassist import create_app


async def verify(output, model):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "model": model, "providerMock": False, "hardwareMeasured": False, "errors": []}
    events = []
    bridge = None
    try:
        async def emit(event):
            events.append({**event, "observedMonotonic": time.perf_counter()})
        async def transcript(*args):
            pass
        bridge = OmniBrowserBridge(os.environ["QWEN_API_KEY"], model, "Ethan",
                                   "Use inspect_project_file to inspect the requested project file. Never invent a file measurement.", emit, transcript)
        bridge.auto_approve_tools = True
        bridge.tool_model = ToolModel(os.environ["QWEN_API_KEY"])
        harness = ContextHarness(SessionJournal(output / "tool-loop.jsonl"))
        bridge.set_context_harness(harness)
        declaration = [{"name": "inspect_project_file", "description": "Read the project README and return its real SHA256 and byte count.",
                        "handler": "test.read-file", "requires_confirmation": True,
                        "arguments": {"type": "object", "properties": {"path": {"type": "string", "enum": ["README.md"]}},
                                      "required": ["path"], "additionalProperties": False}}]
        def inspect_file(arguments, declaration):
            content = (ROOT / "README.md").read_bytes()
            return {"status": "completed", "path": "README.md", "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        executor = SkillToolExecutor({"test.read-file": inspect_file})
        bridge.set_tools(declaration, lambda name, arguments, confirmed: executor.execute(name, arguments, declaration, confirmed))
        await bridge.connect()
        assert bridge.connected
        started = time.perf_counter()
        bridge.conversation.create_item({"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "请使用 inspect_project_file 工具读取 README.md 文件，告诉我真实的文件大小。"}]})
        bridge.request_response("请调用 inspect_project_file 读取 README.md 文件，获得工具结果后简短回答真实字节数。禁止编造文件大小。",
                                origin={"turnId": "real-native-file-inspection"})
        async with asyncio.timeout(60):
            while not (executor.audit and any(e.get("type") == "assistant_transcript" and e.get("done") for e in events)):
                await asyncio.sleep(0.05)
        results = [e for e in harness.journal.entries() if e.get("type") == "tool_result"]
        assert results and results[-1]["result"]["status"] == "completed", results
        assert len(executor.audit) == 1
        assert not any(e.get("type") == "tool_confirmation_required" for e in events)
        report["textToolApi"] = {"model": bridge.tool_model.model, "result": results[-1], "wallMs": round((time.perf_counter()-started)*1000, 1),
                            "executionCount": len(executor.audit),
                            "reply": [e["text"] for e in events if e.get("type") == "assistant_transcript" and e.get("done")]}
        expected = str(results[-1]["result"]["bytes"])
        assert any(expected in reply.replace(",", "") for reply in report["textToolApi"]["reply"]), report["textToolApi"]
        report["textToolApi"]["selectionMs"] = [e["latencyMs"] for e in harness.journal.entries() if e.get("type") == "tool.selection"]
        await bridge.close()
        bridge = None
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), model, "Ethan")
        async with TestServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width":1440, "height":1000})
                page.on("pageerror", lambda error: report["errors"].append(str(error)))
                async def unexpected_dialog(dialog):
                    report["errors"].append("Unexpected blocking dialog: " + dialog.message)
                    await dialog.dismiss()
                page.on("dialog", unexpected_dialog)
                await page.goto(str(server.make_url("/")))
                await page.locator("#registerMode").click()
                await page.locator("#authUsername").fill("real-tool-test")
                await page.locator("#authPassword").fill("isolated-tool-test-123")
                await page.locator("#authSubmit").click()
                await page.wait_for_function("state?.sessionId")
                await page.locator('a[href="#/tasks"]').click()
                await page.locator("#skillSelect").select_option("assembly-coach")
                await page.wait_for_function("state?.activeSkill?.name === 'assembly-coach'")
                await page.locator("#taskSelect").select_option("water-filter")
                await page.wait_for_function("state?.task?.task?.id === 'water-filter'")
                await page.locator('a[href="#/conversation"]').click()
                await page.locator("#textTurnInput").fill("请调用工具打开当前步骤的参考资料。")
                await page.locator("#textTurnSend").click()
                await page.wait_for_function("state?.toolActivity?.some(c => c.tool === 'show_reference' && c.status === 'resolved')", timeout=60000)
                report["browserTools"] = await page.evaluate("state.toolActivity")
                turn_id = report["browserTools"][0]["origin"]["turnId"]
                await page.wait_for_function("id => state.workLedger.turnInbox.some(t => t.turnId === id && t.answer?.status === 'completed')", arg=turn_id, timeout=60000)
                report["browserAnswer"] = await page.evaluate("id => state.workLedger.turnInbox.find(t => t.turnId === id).answer", turn_id)
                assert not await page.evaluate("state.toolActivity.some(c => c.request)")
                assert await page.locator("#toolActivity [data-call-id]").count() > 0
                await page.locator("#toolActivity [data-tool-artifact]").first.click()
                await page.wait_for_function("document.querySelector('#artifactDialog')?.open && artifactDocument && document.querySelector('#artifactCanvas').width > 0")
                report["referenceViewed"] = True
                await page.locator("#artifactClose").click()
                assert await page.evaluate("state.toolApproval") == "automatic"
                await page.reload()
                await page.wait_for_function("state?.toolActivity?.some(c => c.status === 'resolved')", timeout=20000)
                report["restored"] = True
                for width in (1440, 390):
                    await page.set_viewport_size({"width":width, "height":1000})
                    await page.locator("#toolActivity").scroll_into_view_if_needed()
                    await page.screenshot(path=str(output / f"tools-{width}.png"))
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert not report["errors"], report["errors"]
            finally:
                await browser.close()
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if bridge:
            await bridge.close()
    (output / "events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed":report["passed"], "error":report.get("error")}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="qwen3.5-omni-flash-realtime")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output, args.model)) else 1)
