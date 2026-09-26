"""Actual observation connect button and provider, with one labelled UI failure."""
import asyncio
import base64
import io
import json
from pathlib import Path
import secrets
import sys

import av
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


async def main():
    output = ROOT / (sys.argv[1] if len(sys.argv)>1 else 'outputs/ambient-connect-20260926')
    output.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'realProvider': True, 'hardwareMeasured': False,
              'injectedConditions': ['one connection action failure'], 'pageErrors': []}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel='msedge', headless=True)
        try:
            page = await browser.new_page()
            page.on('pageerror', lambda e: report['pageErrors'].append(str(e)))
            await page.goto('http://127.0.0.1:8770/')
            await page.locator('#registerMode').click()
            await page.locator('#authUsername').fill('observe-connect-'+secrets.token_hex(4))
            await page.locator('#authPassword').fill(secrets.token_urlsafe(24))
            await page.locator('#authSubmit').click()
            await page.wait_for_function('state?.sessionId')
            await page.locator('#ambientGrant').click()
            await page.wait_for_function('ambientState?.consented && !ambientPanel.busy')
            assert '尚未连接' in await page.locator('#ambientLiveStatus').inner_text()
            await page.evaluate("()=>{window.originalConnect=ambientPanel.actions.connect;ambientPanel.actions.connect=async()=>{throw Error('测试：连接失败')}}")
            await page.locator('#ambientConnect').click()
            await page.wait_for_function("document.querySelector('#ambientConnect').textContent==='重试连接'")
            assert await page.locator('#ambientConnect').is_enabled()
            await page.evaluate('()=>{ambientPanel.actions.connect=window.originalConnect}')
            await page.locator('#ambientConnect').click()
            await page.wait_for_function('providerReady && ambientState.connected && !ambientPanel.connecting', timeout=70000)
            assert '等待摄像头画面' in await page.locator('#ambientLiveStatus').inner_text()
            assert await page.evaluate('stream===undefined || stream===null')
            report['connectWithoutDeviceCapture'] = True
            # A page reload retains consent but must present an actionable connect button.
            await page.reload()
            await page.wait_for_function('ambientState?.consented && !ambientState.connected', timeout=15000)
            await page.locator('#ambientConnect').click()
            await page.wait_for_function('providerReady && ambientState.connected && !ambientPanel.connecting', timeout=70000)
            report['reloadAndReconnect'] = True
            recording = ROOT / 'data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4'
            with av.open(str(recording)) as video:
                frame = next(video.decode(video=0))
                buffer = io.BytesIO()
                frame.to_image().resize((768, 432)).save(buffer, format='JPEG')
            await page.evaluate('image=>socket.send(JSON.stringify({type:"video",image}))', base64.b64encode(buffer.getvalue()).decode())
            await page.wait_for_function('ambientState?.observation?.summary', timeout=60000)
            report['recordedFrameObservation'] = await page.evaluate('({summary:ambientState.observation.summary,frameId:ambientState.observation.frameId})')
            await page.locator('#ambientRevoke').click()
            await page.wait_for_function('!ambientState.consented && !ambientPanel.busy')
            assert await page.locator('#ambientConnect').is_hidden()
            report['stopStillWorks'] = True
            report['passed'] = not report['pageErrors']
        except Exception as exc:
            report['error'] = type(exc).__name__+': '+str(exc)[:1500]
        finally:
            await browser.close()
    (output/'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return report['passed']


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
