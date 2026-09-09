"""Real Qwen memory/report calls and browser regressions for the memory workspace."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app


async def verify(output):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "providerMock": False, "hardwareMeasured": False, "errors": []}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), "qwen3.5-omni-flash-realtime", "Ethan")
    try:
        async with TestServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: report["errors"].append(str(error)))
                await page.goto(str(server.make_url("/")))
                await page.locator("#registerMode").click()
                await page.locator("#authUsername").fill("memory-workspace-real")
                await page.locator("#authPassword").fill("isolated-workspace-real-123")
                await page.locator("#authSubmit").click()
                await page.locator("#authGate").wait_for(state="hidden")
                await page.wait_for_function("state?.sessionId")
                # Keyboard-only checks do not fabricate provider responses.
                entry = page.locator("#textTurnInput")
                await entry.fill("中文选字")
                await entry.dispatch_event("keydown", {"key": "Enter", "code": "Enter", "isComposing": True})
                assert await entry.input_value() == "中文选字"
                assert await page.evaluate("currentTurnId") is None
                await entry.press("End")
                await entry.press("Shift+Enter")
                assert "\n" in await entry.input_value()
                assert await page.evaluate("currentTurnId") is None
                report["imeAndNewline"] = True

                async def question(text, memory_write=False):
                    before = await page.evaluate("currentTurnId")
                    started = time.perf_counter()
                    await entry.fill(text)
                    await entry.press("Enter")
                    await page.wait_for_function("old => currentTurnId && currentTurnId !== old", arg=before, timeout=40000)
                    tid = await page.evaluate("currentTurnId")
                    await page.wait_for_function("args => state?.workLedger?.turnInbox.some(t => t.turnId === args.id && (t.answer?.status === 'completed' && t.answer?.text || args.memoryWrite && t.delivery?.channel === 'memory-receipt' && t.delivery?.memoryStatus === 'committed'))", arg={"id": tid, "memoryWrite": memory_write}, timeout=65000)
                    turn = await page.evaluate("id => state.workLedger.turnInbox.find(t => t.turnId === id)", tid)
                    assert sum(t["turnId"] == tid for t in await page.evaluate("state.workLedger.turnInbox")) == 1
                    return {"turn": turn, "wallMs": round((time.perf_counter() - started) * 1000, 1)}

                code = "M" + uuid4().hex[:6].upper()
                text = f"请记住：今天张岚和李宁在咖啡馆约定，明天由李宁把编号{code}的青色样机交给王梅。请简短确认。"
                report["write"] = await question(text, memory_write=True)
                await page.wait_for_function("code => memoryRecords(state.episodicMemory).some(c => (c.text || c.summary || '').includes(code))", arg=code, timeout=30000)
                card_id = await page.evaluate("code => memoryRecords(state.episodicMemory).find(c => (c.text || c.summary || '').includes(code)).id", code)
                card = page.locator(f'[data-memory-id="{card_id}"]')
                await card.locator(".memory-record > summary").click()
                await card.locator(".memory-record-body details > summary").click()
                await page.evaluate("window.memoryNodeBefore = document.querySelector('.memory-record[open]')")
                await page.locator("[data-inspect-turn]").first.click()
                await page.locator('[data-disclosure="semantic"] > summary').click()
                report["recall"] = await question("张岚和李宁在咖啡馆约定的样机编号是什么，由谁交给谁？请简短回答。")
                assert code in report["recall"]["turn"]["answer"]["text"], report["recall"]
                assert "王梅" in report["recall"]["turn"]["answer"]["text"]
                await page.wait_for_function("state.episodicMemory.retrieval?.injection?.evidence?.length > 0")
                await page.wait_for_timeout(4200)
                assert await card.locator(".memory-record").evaluate("node => node.open")
                assert await card.locator(".memory-record-body details").evaluate("node => node.open")
                assert await page.locator('[data-disclosure="semantic"]').evaluate("node => node.open")
                assert await page.evaluate("inspectedTurnId") == report["write"]["turn"]["turnId"]
                await page.evaluate("window.memoryNodeBefore = document.querySelector('.memory-record[open]'); render(state)")
                assert await page.evaluate("window.memoryNodeBefore === document.querySelector('.memory-record[open]')")
                report["stableMemoryAndWorkflow"] = True
                await page.locator("#memorySearch").fill(code)
                assert await page.locator("#timelineCards > article:visible").count() >= 1
                await page.locator("#memorySearch").fill("definitely-no-such-memory-xyz")
                assert await page.locator("#timelineCards > article:visible").count() == 0
                await page.locator("#memorySearch").clear()
                assert await card.locator(".memory-record").evaluate("node => node.open")
                report["memory"] = await page.evaluate("state.episodicMemory")

                await page.evaluate("window.socketBeforeNavigation = socket")
                await page.locator('a[href="#/reports"]').click()
                assert await page.locator("#timelineCards").is_hidden()
                await page.locator("#insightGenerate").click()
                await page.wait_for_function("insightJobs.length > 0")
                job_id = await page.evaluate("insightJobs.at(-1).jobId")
                await page.locator('a[href="#/conversation"]').click()
                assert await page.evaluate("socket === window.socketBeforeNavigation && socket.readyState === WebSocket.OPEN")
                assert await card.locator(".memory-record").evaluate("node => node.open")
                report["navigationPreservesConnection"] = True
                # Reload with a report job already accepted; backend owns completion.
                await page.goto(str(server.make_url("/")) + "#/reports")
                await page.locator("#authGate").wait_for(state="hidden")
                await page.wait_for_function("id => insightJobs.some(j => j.jobId === id && ['completed','failed','empty'].includes(j.status))", arg=job_id, timeout=90000)
                job = await page.evaluate("id => insightJobs.find(j => j.jobId === id)", job_id)
                assert job["status"] == "completed", job
                await page.wait_for_function("document.querySelector('#insightResults').textContent.includes('来源')")
                report["reportJob"] = job
                report["reportText"] = await page.locator("#insightResults").inner_text()
                report["reportRestoredAfterReload"] = True
                report["performance"] = await page.evaluate("state.performance")
                for width in (1440, 390):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    for view in ("conversation", "reports", "tasks", "audit"):
                        await page.locator(f'a[href="#/{view}"]').click()
                        await page.evaluate("scrollTo(0,0)")
                        await page.screenshot(path=str(output / f"{view}-{width}.png"), full_page=True)
                        bounds = await page.evaluate("""() => ({width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
                            inputBelowVideo: document.querySelector('#textTurnForm').getBoundingClientRect().top >= document.querySelector('.camera').getBoundingClientRect().bottom,
                            cameraWidth: document.querySelector('.camera').getBoundingClientRect().width,
                            historyScrollable: getComputedStyle(document.querySelector('#turnHistory')).overflowY === 'auto'})""")
                        assert bounds["scrollWidth"] <= width, (view, bounds)
                        if view == "conversation":
                            assert bounds["inputBelowVideo"] and bounds["historyScrollable"] and bounds["cameraWidth"] <= 390, bounds
                        report.setdefault("viewports", []).append({"view": view, **bounds})
                assert not report["errors"], report["errors"]
                report["passed"] = True
            finally:
                await browser.close()
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"memory", "reportText"}}, ensure_ascii=True), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
