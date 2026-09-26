"""Real browser/model test of quiet provenance and explicit reminder recovery."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.async_api import async_playwright
from verify_shared_scene import decode


async def verify(output, base):
    output.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'realProviders': True, 'hardwareMeasured': False, 'pageErrors': []}
    _, frame = decode(ROOT / 'data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4')
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel='msedge', headless=True)
        try:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda e: report['pageErrors'].append(str(e)))
            await page.goto(base + '/')
            await page.locator('#registerMode').click()
            await page.locator('#authUsername').fill('speech-permission-' + secrets.token_hex(4))
            await page.locator('#authPassword').fill(secrets.token_urlsafe(24))
            await page.locator('#authSubmit').click()
            await page.wait_for_function('state?.sessionId')
            report['sessionId'] = await page.evaluate('selectedSessionId')
            await page.locator('#ambientGrant').click()
            await page.wait_for_function('ambientState.consented && !ambientPanel.busy')
            await page.locator('#ambientSettings > summary').click()
            await page.locator('#ambientReminderGoal').fill('看到桌上的红白格桌布时提醒我检查笔记本。')
            await page.locator('#ambientInterval').fill('2')
            await page.locator('#ambientQuestionCooldown').fill('1')
            await page.locator('#ambientSave').click()
            await page.wait_for_function('!ambientPanel.busy && !ambientPanel.dirty')
            quiet_text = '请保持安静，主动提醒也先不要播报。'
            await page.locator('#textTurnInput').fill(quiet_text)
            await page.locator('#textTurnSend').click()
            await page.wait_for_function('state?.interaction?.quiet', timeout=45000)
            report['permissionBefore'] = await page.evaluate('state.interaction')
            await page.locator('#speechPermissionDetails > summary').click()
            assert quiet_text in await page.locator('#speechPermissionReason').inner_text()
            await page.wait_for_function("!state.runtimeLanes.interaction.preparation?.length", timeout=15000)
            await page.evaluate('''() => {
                window.permissionAudio = [];
                socket.addEventListener('message', e => {
                    const m = JSON.parse(e.data);
                    if (m.type === 'audio' && m.origin?.observationReviewId)
                        permissionAudio.push({responseId:m.responseId, reviewId:m.origin.observationReviewId});
                });
            }''')

            async def observe():
                previous = await page.evaluate('state.runtimeLanes.observation.reviews.at(-1)?.candidateId || null')
                await page.evaluate("image => socket.send(JSON.stringify({type:'video',image}))", base64.b64encode(frame).decode())
                await page.wait_for_function('previous => { const row=state.runtimeLanes.observation.reviews.at(-1); return row && row.candidateId!==previous; }',
                                             arg=previous, timeout=45000)
                return await page.evaluate('state.runtimeLanes.observation.reviews.at(-1)')

            blocked = report['quietReview'] = await observe()
            # The real model may itself abstain during quiet. Record that as a
            # distinct branch, never claim it exercised the code delivery gate.
            report['deliveryGateExercised'] = bool(blocked['notification'])
            if report['deliveryGateExercised']:
                assert blocked['reason'] == 'output-not-permitted', blocked
                assert blocked['outputPermission']['quiet']
            else:
                assert blocked['status'] == 'recorded', blocked
            assert not await page.evaluate('permissionAudio.length')
            await page.screenshot(path=str(output / 'permission-before.png'))
            await page.locator('#resumeSpeechPermission').click()
            await page.wait_for_function('state.interaction.quiet===false && !resumingSpeechPermission', timeout=20000)
            report['permissionAfter'] = await page.evaluate('state.interaction')
            assert await page.evaluate('audioContext.state') == 'running'
            assert not await page.evaluate('permissionAudio.length')
            historical = await page.evaluate('id => state.runtimeLanes.observation.reviews.find(r=>r.candidateId===id)', blocked['candidateId'])
            assert historical['status'] == blocked['status']
            assert historical['outputPermission'] == blocked['outputPermission']
            fresh = report['freshReview'] = await observe()
            assert fresh['notification'] and fresh['status'] in {'ready', 'audio-sent', 'played'}, fresh
            await page.wait_for_function('id => state.runtimeLanes.observation.reviews.some(r=>r.candidateId===id && r.status==="played")',
                                         arg=fresh['candidateId'], timeout=35000)
            report['browserPlaybackConfirmed'] = True
            assert await page.evaluate('id => permissionAudio.every(e=>e.reviewId!==id)', blocked['candidateId'])
            await page.reload()
            await page.wait_for_function('state?.interaction?.quiet===false')
            assert await page.locator('#resumeSpeechPermission').is_hidden()
            await page.locator('#speechPermissionDetails > summary').click()
            await page.screenshot(path=str(output / 'permission-after.png'))
            await page.set_viewport_size({'width': 390, 'height': 844})
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.screenshot(path=str(output / 'permission-mobile.png'))
            assert not report['pageErrors'], report['pageErrors']
            report['passed'] = True
        except Exception as exc:
            report['error'] = type(exc).__name__ + ': ' + str(exc)
        finally:
            await browser.close()
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'quietReview', 'freshReview'}}, ensure_ascii=True))
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:8770')
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output, args.base_url.rstrip('/'))) else 1)
