"""Record the actual diary UI reading workflow against saved real model results."""
import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def main(folder, base):
    report = json.loads((folder / 'verification.json').read_text('utf-8'))
    assert report['passed'] and not report['mock']
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel='msedge', headless=True)
        context = await browser.new_context(viewport={'width': 1440, 'height': 1000},
            record_video_size={'width': 1440, 'height': 1000},
            record_video_dir=str(folder / 'walkthrough'))
        login = await context.request.post(base + '/api/auth/login', data={
            'username': 'diary-demo', 'password': 'Diary-local-demo-20260917'})
        assert login.ok
        page = await context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(base + '/diary.html')
        await page.locator('.claim').first.wait_for()
        for kind in ['daily', 'weekly', 'monthly']:
            await page.evaluate('scrollTo(0,0)')
            await page.select_option('#kind', kind)
            await page.locator('#anchor').fill('2026-09-16')
            await page.locator('#findArchive').click()
            label = {'daily': '日记', 'weekly': '周回顾', 'monthly': '月回顾'}[kind]
            await page.wait_for_function('label=>document.querySelector("#reading .eyebrow")?.textContent===label', arg=label)
            await asyncio.sleep(5)
            claim = page.locator('.claim details').first
            await claim.locator('summary').click()
            await claim.scroll_into_view_if_needed()
            await asyncio.sleep(5)
            await claim.locator('summary').click()
            for title in ['生活', '工作', '进展与变化', '下一步可以做什么']:
                await page.get_by_role('heading', name=title, exact=True).scroll_into_view_if_needed()
                await asyncio.sleep(5)
            if kind == 'monthly':
                await page.locator('[data-feedback]').first.click()
                await page.get_by_text('已保存你的选择。', exact=True).wait_for()
                await page.locator('[data-feedback]').first.scroll_into_view_if_needed()
                await asyncio.sleep(4)
                await page.get_by_text('生成记录 · 6 次处理', exact=True).click()
                await page.locator('#reading pre').scroll_into_view_if_needed()
                await asyncio.sleep(5)
            await page.evaluate('scrollTo(0,0)')
            await page.locator('#allArchive').click()
        await page.reload()
        await page.locator('.claim').first.wait_for()
        await asyncio.sleep(5)
        await page.set_viewport_size({'width': 390, 'height': 1000})
        await asyncio.sleep(5)
        assert not errors, errors
        video = page.video
        await context.close()
        report['walkthroughRecording'] = Path(await video.path()).relative_to(folder).as_posix()
        await browser.close()
    (folder / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(report['walkthroughRecording'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:8779')
    args = parser.parse_args()
    asyncio.run(main(args.source.resolve(), args.base_url))
