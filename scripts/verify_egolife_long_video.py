"""Verify full decoding, browser playback and every long-recording chapter."""
import argparse
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright
import verify_story_videos as base


async def main(output):
    base.OUTPUT=output
    await base.main()
    manifest=json.loads((output/'manifest.json').read_text('utf-8'))
    entry=manifest['videos'][0]
    assert manifest['uncut'] and entry['durationSeconds']>600
    chapters=entry['chapters'];results=[]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(channel='msedge',headless=True)
        page=await browser.new_page(viewport={'width':1440,'height':1000})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        await page.goto((output/'index.html').as_uri())
        await page.wait_for_function('document.querySelector("video").readyState>=1')
        assert await page.locator('button').count()==len(chapters)==6
        for i,chapter in enumerate(chapters):
            await page.locator('button').nth(i).click()
            await page.wait_for_function('t=>{const v=document.querySelector("video");return v.readyState>=2 && Math.abs(v.currentTime-t)<2}',arg=chapter['startSeconds'],timeout=15000)
            info=await page.locator('video').evaluate('(v)=>({currentTime:v.currentTime,width:v.videoWidth,height:v.videoHeight,error:v.error?.message||null})')
            assert info['width']==1920 and info['height']==1080 and info['error'] is None
            results.append({**chapter,**info})
            await page.locator('video').evaluate('(v)=>v.pause()')
        await page.screenshot(path=str(output/'chapter-final.png'),full_page=True)
        await page.locator('summary').click()
        for width in (1440,390):
            await page.set_viewport_size({'width':width,'height':1000})
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.screenshot(path=str(output/f'comparison-{width}.png'),full_page=True)
        assert not errors,errors
        await browser.close()
    report=json.loads((output/'verification.json').read_text('utf-8'))
    report.update(chapters=results,chapterSeekingPassed=True,expandedComparisonWidths=[1440,390],localFilePlayback=True)
    (output/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'chapterCount':len(results),'localFilePlayback':True}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    asyncio.run(main(p.parse_args().output.resolve()))
