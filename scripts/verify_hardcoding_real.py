"""Real Qwen memory, dependent tool calls and browser audit acceptance."""
import argparse
import asyncio
import base64
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
from egoassist.realtime.tool_model import ToolModel
from egoassist.realtime.tools import SkillToolExecutor
from run_egoassist import create_app


async def verify(output):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "providerMock": False, "hardwareMeasured": False, "errors": [], "turns": []}
    bridge = None
    try:
        events = []
        async def emit(event):
            events.append(event)
        async def transcript(*args):
            pass
        bridge = OmniBrowserBridge(os.environ["QWEN_API_KEY"], "qwen3.5-omni-flash-realtime", "Ethan",
            "Use real tool receipts, never invent file values.", emit, transcript)
        bridge.tool_model = ToolModel(os.environ["QWEN_API_KEY"])
        bridge.auto_approve_tools = True
        harness = ContextHarness(SessionJournal(output / "dependent-tools.jsonl"))
        bridge.set_context_harness(harness)
        digest = hashlib.sha256((ROOT / "README.md").read_bytes()).hexdigest()
        declarations = [
            {"name": "measure_file", "description": "Read the real README, returning sha256.", "handler": "measure",
             "arguments": {"type": "object", "properties": {}, "additionalProperties": False}},
            {"name": "verify_digest", "description": "Verify the exact sha256 returned by measure_file against the current file.", "handler": "verify",
             "arguments": {"type": "object", "properties": {"sha256": {"type": "string"}}, "required": ["sha256"], "additionalProperties": False}},
        ]
        def measure(arguments, declaration):
            return {"status": "completed", "sha256": hashlib.sha256((ROOT / "README.md").read_bytes()).hexdigest()}
        def check(arguments, declaration):
            actual = hashlib.sha256((ROOT / "README.md").read_bytes()).hexdigest()
            assert arguments["sha256"] == actual
            return {"status": "completed", "matched": True, "sha256": actual}
        executor = SkillToolExecutor({"measure": measure, "verify": check})
        bridge.set_tools(declarations, lambda name, arguments, confirmed: executor.execute(name, arguments, declarations, confirmed))
        await bridge.connect()
        started = time.perf_counter()
        bridge.request_response("先用 measure_file 读取 README 的 SHA256，再把实际返回的 SHA256 传给 verify_digest 校验。完成后只说校验是否成功。",
                                origin={"turnId": "dependent-tools"})
        async with asyncio.timeout(60):
            while not any(e.get("type") == "assistant_transcript" and e.get("done") for e in events):
                await asyncio.sleep(0.05)
        results = [r for r in harness.journal.entries() if r["type"] == "tool_result"]
        assert [r["name"] for r in results] == ["measure_file", "verify_digest"], results
        assert results[-1]["result"]["sha256"] == digest and results[-1]["result"]["matched"]
        report["dependentTools"] = {"wallMs": round((time.perf_counter() - started) * 1000, 1),
            "results": results, "reply": [e["text"] for e in events if e.get("type") == "assistant_transcript" and e.get("done")],
            "selectionMs": [r["latencyMs"] for r in harness.journal.entries() if r["type"] == "tool.selection"]}
        await bridge.close()
        bridge = None
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), "qwen3.5-omni-flash-realtime", "Ethan")
        async with TestServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: report["errors"].append(str(error)))
                await page.goto(str(server.make_url("/")))
                await page.locator("#registerMode").click()
                await page.locator("#authUsername").fill("hardcoding-real")
                await page.locator("#authPassword").fill("isolated-hardcoding-123")
                await page.locator("#authSubmit").click()
                await page.wait_for_function("state?.sessionId")
                for text in [
                    "这件事替我留个记录：今天没有吃过药，今天吃了一个苹果。不要改成已经服药。",
                    "替我留个记录：小林的工位在三楼；蓝色样机归陈澄保管；项目代号是晨光；交付对象是周岳；包装颜色是绿色；会议地点是二号实验室。保留全部六项。",
                    "我今天服过药吗？只根据刚才记录简短回答。",
                ]:
                    before = await page.evaluate("state.workLedger.turnInbox.length")
                    started = time.perf_counter()
                    await page.locator("#textTurnInput").fill(text)
                    await page.locator("#textTurnInput").press("Enter")
                    await page.wait_for_function("n => state.workLedger.turnInbox.length > n", arg=before, timeout=20000)
                    await page.wait_for_function("text => state.workLedger.turnInbox.some(t => t.text === text && (['completed','not-required'].includes(t.answer?.status) || t.delivery?.status === 'completed'))",
                        arg=text, timeout=60000)
                    turn = await page.evaluate("text => state.workLedger.turnInbox.find(t => t.text === text)", text)
                    report["turns"].append({"text": text, "wallMs": round((time.perf_counter()-started)*1000,1), "turn": turn})
                await page.wait_for_function("JSON.stringify(state.episodicMemory.timelineViews.participant.recent).includes('二号实验室')", timeout=60000)
                report["memory"] = await page.evaluate("state.episodicMemory.timelineViews.participant.recent")
                combined = json.dumps(report["memory"], ensure_ascii=False)
                for label in ("没有", "苹果", "小林", "陈澄", "晨光", "周岳", "绿色", "二号实验室"):
                    assert label in combined, label
                reply = report["turns"][-1]["turn"]["answer"]["text"]
                assert "没有" in reply or "未" in reply, reply
                from verify_real_voice_stop import synthesize
                spoken = "助手，替我留个记录，橙色笔记本放在书架第二层。"
                pcm, fixture = await synthesize(spoken, output, "memory")
                before = await page.evaluate("state.workLedger.turnInbox.length")
                voice_started = time.perf_counter()
                data = pcm + bytes(32000)
                for offset in range(0, len(data), 3200):
                    await page.evaluate("audio => socket.send(JSON.stringify({type:'audio', audio}))",
                        base64.b64encode(data[offset:offset + 3200]).decode())
                    await asyncio.sleep(0.1)
                sent_at = time.perf_counter()
                await page.wait_for_function("n => state.workLedger.turnInbox.length > n", arg=before, timeout=25000)
                await page.wait_for_function("JSON.stringify(state.episodicMemory.timelineViews.participant.recent).includes('第二层')", timeout=45000)
                report["voice"] = {"fixture": fixture, "wallMs": round((time.perf_counter()-voice_started)*1000,1),
                    "afterAudioMs": round((time.perf_counter()-sent_at)*1000,1),
                    "turns": await page.evaluate("n => state.workLedger.turnInbox.slice(n)", before),
                    "memory": await page.evaluate("state.episodicMemory.timelineViews.participant.recent")}
                await page.locator('a[href="#/audit"]').click()
                await page.evaluate("document.querySelector('#promptAuditDetails').open = true; refreshPromptAudit()")
                await page.wait_for_function("document.querySelectorAll('#actualPromptRequests details').length > 0")
                item = page.locator("#actualPromptRequests details").first
                await item.evaluate("el => el.open = true")
                event_id = await item.get_attribute("data-id")
                await page.evaluate("refreshPromptAudit()")
                await page.wait_for_timeout(500)
                assert await page.locator(f'#actualPromptRequests details[data-id="{event_id}"]').get_attribute("open") is not None
                audit = await page.evaluate("request('/api/prompts?session=' + selectedSessionId + '&limit=2')")
                assert audit["nextBefore"] and len(audit["requests"]) == 2
                older = await page.evaluate("cursor => request('/api/prompts?session=' + selectedSessionId + '&limit=2&before=' + cursor)", audit["nextBefore"])
                assert not {e["id"] for e in older["requests"]} & {e["id"] for e in audit["requests"]}
                report["auditPagination"] = True
                report["configuration"] = audit["configuration"]
                for width in (1440, 390):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    await page.locator("#promptAuditDetails").scroll_into_view_if_needed()
                    await page.screenshot(path=str(output / f"audit-{width}.png"))
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                await page.reload()
                await page.wait_for_function("state?.workLedger?.turnInbox?.length >= 3")
                report["restored"] = True
                assert not report["errors"]
            finally:
                await browser.close()
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if bridge:
            await bridge.close()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({"passed": report["passed"], "error": report.get("error")}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(0 if asyncio.run(verify(parser.parse_args().output)) else 1)
