"""Browser fault injection for UI races; no model or hardware validation claims."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from run_egoassist import create_app


async def verify(output):
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "faultInjection": True, "modelValidation": False, "errors": []}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), "qwen3.5-omni-flash-realtime", "Ethan")
    try:
        async with TestServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: report["errors"].append(str(error)))
                await page.goto(str(server.make_url("/")))
                await page.locator("#registerMode").click()
                await page.locator("#authUsername").fill("console-races")
                await page.locator("#authPassword").fill("isolated-browser-races-123")
                await page.locator("#authSubmit").click()
                await page.wait_for_function("state?.sessionId && !insightPolling")
                report["races"] = await page.evaluate(r"""async () => {
                    const check = (value, message) => { if (!value) throw Error(message); };
                    const savedConnect = connectRealtime, savedRequest = request;
                    const savedAdd = TurnOutbox.prototype.add;
                    const a = selectedSessionId, snapshotA = state;
                    const b = (await post('/api/sessions', {label:'Race B'})).session.id;
                    const sent = [], modes = [];
                    let release;
                    try {
                        // Deliberately hold connection readiness across an actual DOM change event.
                        connectRealtime = () => {
                            const pending = new Promise(resolve => { release = resolve; });
                            connectingPromise = pending;
                            return pending;
                        };
                        $('#textTurnInput').value = 'original A draft';
                        const submission = $('#textTurnForm').onsubmit({preventDefault(){}});
                        await refreshSessionList(a);
                        $('#sessionSelect').value = b;
                        $('#sessionSelect').dispatchEvent(new Event('change'));
                        connectRealtime = async options => {
                            modes.push(options.media);
                            socket = {readyState:WebSocket.OPEN, bufferedAmount:0,
                                send: value => sent.push(JSON.parse(value)), close(){}};
                        };
                        while (sessionSwitchBusy) await new Promise(resolve => setTimeout(resolve, 20));
                        // Switching must finish even though the old provider is still pending.
                        release();
                        await submission;
                        check(selectedSessionId === b, 'DOM target lost after await');
                        check(!sent.some(item => item.type === 'control'), 'A draft sent to B');
                        check($('#textTurnInput').value === '', 'A draft leaked into B composer');
                        check(modes.length === 1 && modes[0] === false, 'Text switch requested media');
                        check(!$('#textTurnSend').disabled, 'Send stayed disabled');
                        render(snapshotA);
                        check(state.sessionId === b, 'Old snapshot overwrote B');
                        await changeSession(a);
                        check($('#textTurnInput').value === 'original A draft', 'A draft was lost');
                        // A local persistence failure must not fabricate a server receipt.
                        const count = localSubmittedTurns.size;
                        TurnOutbox.prototype.add = () => {throw Error('injected storage failure');};
                        await $('#textTurnForm').onsubmit({preventDefault(){}});
                        check(localSubmittedTurns.size === count, 'Storage failure added phantom turn');
                        check($('#textTurnInput').value === 'original A draft', 'Storage failure lost draft');
                        TurnOutbox.prototype.add = savedAdd;
                        disconnectRealtime();
                        // Resolve the newer report first, then deliver the stale one.
                        const pending = {};
                        request = path => {
                            const id = new URL(path, location.href).searchParams.get('result');
                            return id ? new Promise(resolve => {pending[id] = resolve;}) : savedRequest(path);
                        };
                        const result = (id, kind) => ({id, period:{kind, anchor:'2026-09-10'}, status:'completed',
                            summary:[], suggestions:[], sources:[], coverage:{}});
                        const older = loadInsightResult('old'), newer = loadInsightResult('new');
                        pending.new({results:[result('new','weekly')]}); await newer;
                        pending.old({results:[result('old','daily')]}); await older;
                        check(displayedInsightResult === 'new', 'Stale report won race');
                        check($('#insightResultTitle').textContent === '\u5468\u62a5 \u00b7 2026-09-10', 'Wrong report schema');
                        request = savedRequest;
                        socket = {readyState:WebSocket.OPEN, bufferedAmount:0, send(){}, close(){}};
                        stateRefreshPendingAt = Date.now() - 9000;
                        lastDetachedStatePoll = 0;
                        const deadline = Date.now() + 12000;
                        while (stateRefreshPendingAt && Date.now() < deadline) {
                            await new Promise(resolve => setTimeout(resolve, 20));
                        }
                        check(!stateRefreshPendingAt, 'Stalled WebSocket refresh did not recover through HTTP');
                        disconnectRealtime();
                        return {sessionIsolation:true, draftRestored:true, textOnlySwitch:true,
                            staleStateIgnored:true, storageFailurePreservesInput:true, reportOrdering:true,
                            pendingConnectionDoesNotBlockSwitch:true, httpRefreshRecovery:true};
                    } finally {
                        connectRealtime = savedConnect; request = savedRequest;
                        TurnOutbox.prototype.add = savedAdd; disconnectRealtime();
                    }
                }""")
                for width in (1440, 390):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    await page.screenshot(path=str(output / f"viewport-{width}.png"), full_page=True)
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert not report["errors"], report["errors"]
                report["passed"] = True
            finally:
                await browser.close()
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
