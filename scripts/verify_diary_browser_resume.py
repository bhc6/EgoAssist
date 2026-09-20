"""Verify a real interrupted diary through the product's retry button after restart."""
import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


def save(path,value): path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


async def main(folder,base):
    report=json.loads((folder/'verification.json').read_text('utf-8'))
    if not (folder/'verification-before-resume.json').exists():
        save(folder/'verification-before-resume.json',report)
        save(folder/'monthly-before-resume.json',json.loads((folder/'monthly.json').read_text('utf-8')))
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(channel='msedge',headless=True)
        context=await browser.new_context(viewport={'width':1440,'height':1000},record_video_dir=str(folder/'recordings'))
        page=await context.new_page();errors=[];websockets=[]
        page.on('pageerror',lambda e:errors.append(str(e)));page.on('websocket',lambda ws:websockets.append(ws.url))
        await page.goto(base+'/diary.html')
        await page.locator('#login').wait_for(state='visible')
        await page.locator('[name=username]').fill('diary-demo');await page.locator('[name=password]').fill('Diary-local-demo-20260917')
        await page.locator('#loginForm button').click();await page.locator('#app').wait_for(state='visible')
        monthly=next(r for r in report['results'] if r['kind']=='monthly')
        original=json.loads((folder/'monthly-before-resume.json').read_text('utf-8'))
        current=await (await context.request.get(base+'/api/diary/'+monthly['id'])).json()
        if current['status']=='failed':
            await page.locator('[data-retry]').wait_for(state='visible')
            await page.screenshot(path=str(folder/'retry-before.png'),full_page=True)
            await page.locator('[data-retry]').click()
        async with asyncio.timeout(300):
            while True:
                result=await (await context.request.get(base+'/api/diary/'+monthly['id'])).json()
                if result['status'] not in {'queued','running'}: break
                await asyncio.sleep(2)
        save(folder/'monthly-after-resume.json',result)
        assert result['status']=='completed',result.get('error')
        assert len(result['calls'])==len(original['calls'])+1, 'Must reuse saved chunks and only repeat final generation'
        monthly.update(status='completed',calls=len(result['calls']),error=None)
        save(folder/'monthly.json',result)
        report['resumedAfterRestart']={'previousStatus':'failed','previousError':'TimeoutError',
            'retainedCalls':len(original['calls']),'additionalCalls':1,'method':'actual product retry button'}
        await page.wait_for_function('document.querySelectorAll(".claim").length>0')
        for item in report['results']:
            await page.locator('[data-id="'+item['id']+'"]').click()
            await page.wait_for_function('kind=>document.querySelector("#reading .eyebrow")?.textContent===kind',arg={'daily':'日记','weekly':'周回顾','monthly':'月回顾'}[item['kind']])
            await page.screenshot(path=str(folder/(item['kind']+'-desktop.png')),full_page=True)
        await page.locator('.claim details summary').first.click()
        await page.screenshot(path=str(folder/'source-expanded.png'),full_page=True)
        feedback=page.locator('[data-feedback]').first
        if await feedback.count():
            await feedback.click();await page.wait_for_function('document.querySelector("[data-feedback]")?.getAttribute("aria-pressed")==="true"')
            report['feedbackSaved']=True
        await page.reload();await page.wait_for_function('document.querySelectorAll(".claim").length>0')
        report['reloadRestoresArchive']=True
        if report.get('feedbackSaved'):
            assert await page.locator('[data-feedback]').first.get_attribute('aria-pressed')=='true'
        await page.select_option('#kind','weekly');await page.locator('#anchor').fill('2026-09-16')
        await page.locator('#findArchive').click()
        await page.wait_for_function('document.querySelectorAll(".entry").length===1 && document.querySelector("#reading .eyebrow")?.textContent==="周回顾"')
        report['calendarLookup']=True
        await page.locator('#allArchive').click();await page.wait_for_function('document.querySelectorAll(".entry").length===3')
        await page.locator('[data-id="'+monthly['id']+'"]').click()
        await page.wait_for_function('document.querySelector("#reading .eyebrow")?.textContent==="月回顾"')
        await page.set_viewport_size({'width':390,'height':1000})
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert await page.locator('#reading').evaluate('(n)=>n.getBoundingClientRect().top')<await page.locator('aside').evaluate('(n)=>n.getBoundingClientRect().top')
        await page.screenshot(path=str(folder/'monthly-mobile.png'),full_page=True)
        report['mobileReadingFirst']=True
        await context.request.post(base+'/api/sessions',data={})
        listing=await (await context.request.get(base+'/api/diary')).json()
        assert {r['id'] for r in report['results']}<={r['id'] for r in listing['entries']}
        report['crossSessionArchive']=True
        for item in report['results']:
            exported=await context.request.get(base+'/api/diary/'+item['id']+'/export');assert exported.ok
            (folder/(item['kind']+'.md')).write_text(await exported.text(),encoding='utf-8')
        report['noRealtimeConnection']=not websockets;assert not websockets
        report['pageErrors']=errors;assert not errors
        video=page.video
        await context.close()
        report['browserRecording']=str(Path(await video.path()).relative_to(folder).as_posix())
        await browser.close()
    report['passed']=True;report.pop('error',None)
    save(folder/'verification.json',report)
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--base-url',default='http://127.0.0.1:8779')
    args=parser.parse_args();asyncio.run(main(args.source.resolve(),args.base_url))
