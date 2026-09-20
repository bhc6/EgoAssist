"""Verify readable labels and renaming using copied real records in an isolated app."""
import argparse
import asyncio
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestClient, TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.core.journal import SessionJournal
from run_egoassist import create_app
from egoassist._env import load_env
from egoassist.realtime.long_events import LongEvents
import os


async def verify(source, output, derive_events=False, reuse_events=False):
    load_env(str(ROOT / '.env'))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "source": str(source.resolve()), "isolatedCopy": True, "modelCalls": 0, "pageErrors": []}
    def app():
        return create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"), "qwen3.5-omni-flash-realtime", "Ethan")
    credentials = {"username": "speaker-ui-check", "password": "speaker-ui-check-20260918"}
    async with TestClient(TestServer(app())) as client:
        assert (await client.post("/api/auth/register", json=credentials)).status == 200
        sid = (await (await client.post("/api/sessions", json={"label": "声音命名验证"})).json())["session"]["id"]
        assert (await client.get("/api/state", params={"session": sid})).status == 200
    path = next(output.glob(f"users/*/sessions/{sid}.jsonl"))
    journal = SessionJournal(path, session_id=sid)
    source_rows = [json.loads(line) for line in source.open(encoding='utf-8')]
    reused_source_ids = {ref for row in source_rows if row['type']=='long_event.batch' for ref in row['processedSourceIds']}
    for row in source_rows:
        if reuse_events and row['type'] in {'ambient_observation','message'} and row['id'] not in reused_source_ids:
            continue  # UI replay freezes the already-derived evidence snapshot.
        kinds = {'speaker.profile', 'scene.turn'} | ({'ambient_observation','message'} if derive_events or reuse_events else {'turn_inbox'})
        if reuse_events: kinds |= {'long_event.batch', 'long_event.reset', 'long_event.job'}
        if row['type'] in kinds:
            journal.append(row['type'], {k:v for k,v in row.items() if k != 'type'})
        if row["type"] == "scene.turn" and row.get("audio"):
            audio = row["audio"]["assetId"]
            target = path.parent / "media" / sid / audio
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source.parent / "media" / source.stem / audio, target)
        ref = (row.get('evidence') or {}).get('media') if row['type']=='ambient_observation' else None
        if (derive_events or reuse_events) and ref and ref.get('assetId'):
            target=path.parent/'media'/sid/ref['assetId'];target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source.parent/'media'/source.stem/ref['assetId'],target)
    try:
        if derive_events or reuse_events:
            tracker=LongEvents(journal, os.getenv('QWEN_API_KEY') or os.getenv('DASHSCOPE_API_KEY'))
            try:
                if derive_events: await tracker.run(max_batches=20)
                report['reusedEventDerivation']=reuse_events
                report['longEvents']=tracker.snapshot()
                assert report['longEvents']['pendingSources']==0, report['longEvents']['job']
                assert report['longEvents']['events'], 'real observations produced no events'
                assert all(e['coverage']['continuousDurationSeconds'] is None for e in report['longEvents']['events'])
                report['modelCalls']=len(journal.entries(types={'long_event.request'}))
                assert all(r.get('providerRequestId') for r in journal.entries(types={'long_event.response'}))
            finally:
                await tracker.close()
        async with TestServer(app()) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda e: report["pageErrors"].append(str(e)))
                await page.goto(str(server.make_url("/")))
                await page.locator("#authUsername").fill(credentials["username"])
                await page.locator("#authPassword").fill(credentials["password"])
                await page.locator("#authSubmit").click()
                await page.locator("#authGate").wait_for(state="hidden")
                # Restore the copied session through the same selector users use.
                await page.wait_for_function("id => selectedSessionId === id", arg=sid)
                await page.wait_for_function("document.querySelector('#speakerIdentitySelect').options.length > 0")
                report["namesBefore"] = await page.locator("#speakerIdentitySelect option").all_text_contents()
                assert report["namesBefore"][0] == ('测试称呼' if reuse_events else '声音 1')
                assert "speaker-" not in await page.locator("#sharedSpeakers").inner_text()
                await page.locator("#sharedSpeakers").screenshot(path=str(output / "names-before.png"))
                await page.locator("#speakerIdentityName").fill("测试称呼")
                await page.locator("#speakerIdentityForm button").click()
                await page.wait_for_function("state?.sharedScene?.speakers[0]?.displayName === '测试称呼'")
                await page.wait_for_function("[...document.querySelectorAll('#sharedUtterances .utterance-text b')].some(n => n.textContent === '测试称呼')")
                await page.reload()
                await page.wait_for_function("state?.sharedScene?.speakers[0]?.displayName === '测试称呼'")
                report["namesAfterReload"] = await page.locator("#speakerIdentitySelect option").all_text_contents()
                assert report["namesAfterReload"][1:] == report["namesBefore"][1:]
                report["historicalNameUpdated"] = True
                profiles=await page.evaluate('state.sharedScene.speakers')
                assert len(profiles)>=2
                source_id,target_id=profiles[1]['speakerId'],profiles[0]['speakerId']
                before_ids=await page.evaluate('state.sharedScene.utterances.map(r=>r.turnId)')
                await page.locator('#speakerMergeSource').select_option(source_id)
                await page.locator('#speakerMergeTarget').select_option(target_id)
                await page.locator('#speakerMergeForm button').click()
                await page.wait_for_function('n=>state.sharedScene.speakers.length===n',arg=len(profiles)-1)
                await page.reload()
                await page.wait_for_function('n=>state?.sharedScene?.speakers.length===n',arg=len(profiles)-1)
                assert await page.evaluate('state.sharedScene.utterances.map(r=>r.turnId)')==before_ids
                await page.locator('[data-undo-speaker-merge]').click()
                await page.wait_for_function('n=>state.sharedScene.speakers.length===n',arg=len(profiles))
                report['mergeReloadUndoPassed']=True
                report['mergeIdentityClaim']='UI operation on isolated copy; not a claim these real speakers are the same person'
                if derive_events or reuse_events:
                    await page.wait_for_function('state.sharedScene.longEvents.events.length>0')
                    restored=await page.evaluate('state.sharedScene.longEvents')
                    assert {e['eventId'] for e in restored['events']}=={e['eventId'] for e in report['longEvents']['events']}
                    report['eventRestorePassed']=True
                    await page.locator('#longEventTitle').scroll_into_view_if_needed()
                    await page.screenshot(path=str(output/'events.png'))
                    assert await page.locator('#longEvents article').count()>0
                    first=page.locator('#longEvents article').first
                    await first.locator('summary').click()
                    await first.locator('[data-event-evidence]').click()
                    await page.wait_for_function("!document.querySelector('#longEvents article [data-event-evidence]') || !document.querySelector('#longEvents article [data-event-evidence]').disabled")
                    assert await first.locator('.event-sources blockquote').count()>0
                    report['evidencePagePassed']=True
                if derive_events:
                    question='请用不超过一百字，根据事件回顾概括发生了什么。区分观察和自述；有观察中断要明确说，不能把时间跨度当连续时长。'
                    await page.locator('#textTurnInput').fill(question)
                    await page.locator('#textTurnSend').click()
                    await page.wait_for_function("!document.querySelector('#textTurnInput').value && currentTurnId",timeout=60000)
                    tid=await page.evaluate('currentTurnId')
                    await page.wait_for_function("id=>state.workLedger.turnInbox.some(t=>t.turnId===id&&t.answer?.status==='completed')",arg=tid,timeout=90000)
                    report['eventAnswer']=await page.evaluate('id=>state.workLedger.turnInbox.find(t=>t.turnId===id).answer',tid)
                    assert report['eventAnswer'].get('text')
                for width in (1440, 390):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    await page.locator("#speakerIdentityForm").scroll_into_view_if_needed()
                    await page.screenshot(path=str(output / f"renamed-{width}.png"))
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert not report["pageErrors"]
                report["passed"] = True
            finally:
                await browser.close()
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in {'longEvents','eventAnswer'}}, ensure_ascii=True))
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument('--derive-events', action='store_true')
    parser.add_argument('--reuse-events', action='store_true')
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.journal, args.output, args.derive_events, args.reuse_events)) else 1)
