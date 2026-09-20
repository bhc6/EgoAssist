"""Decode every exported video and exercise gallery playback in Edge."""
from __future__ import annotations

import asyncio
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import av
import numpy as np
from PIL import Image
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'outputs/story-videos-20260916'


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def copyfile(self, source, outputfile):
        try:
            super().copyfile(source, outputfile)
        except (ConnectionResetError, BrokenPipeError):
            # Seeking or pausing a video can cancel its HTTP download.
            pass


async def main():
    manifest = json.loads((OUTPUT / 'manifest.json').read_text(encoding='utf-8'))
    checks, samples = [], []
    for entry in manifest['videos']:
        path = OUTPUT / entry['file']
        with av.open(str(path)) as container:
            video = container.streams.video[0]
            assert video.codec_context.name == 'h264'
            assert (video.width, video.height) == (1920, 1080)
            assert container.streams.audio[0].codec_context.name == 'aac'
            count, last, poster = 0, None, None
            sample_time = entry['audio'][-1]['startSeconds'] + .5
            sample = None
            for frame in container.decode(video=0):
                count += 1
                last = frame.time
                if sample is None and frame.time >= sample_time:
                    sample = frame.to_image()
                if poster is None and frame.time >= entry['durationSeconds'] - 2:
                    poster = frame.to_image()
            assert abs(last + .04 - entry['durationSeconds']) < .1
            assert poster is not None and sample is not None
            poster.save(OUTPUT / entry['poster'], quality=92)
            sample.save(OUTPUT / (entry['id'] + '-playback.jpg'), quality=90)
            sample.thumbnail((960, 540))
            samples.append(sample)
        with av.open(str(path)) as container:
            audio = [frame.to_ndarray().reshape(-1) for frame in container.decode(audio=0)]
            signal = np.concatenate(audio)
            peak = float(np.abs(signal).max())
            assert peak > .01
        checks.append(dict(file=entry['file'], fullDecode=True, frames=count,
                           durationSeconds=round(last + .04, 2), videoCodec='h264', audioCodec='aac', audioPeak=peak))
    contact = Image.new('RGB', (1920, 1620), '#102632')
    for i, sample in enumerate(samples):
        contact.paste(sample, (i % 2 * 960, i // 2 * 540))
    contact.save(OUTPUT / 'contact-sheet.jpg', quality=92)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='msedge', headless=True)
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(f'http://127.0.0.1:{server.server_port}/outputs/{OUTPUT.name}/index.html')
            await page.wait_for_load_state('networkidle')
            for index, check in enumerate(checks):
                result = await page.locator('video').nth(index).evaluate('''async video => {
                    video.muted = true;
                    await video.play();
                    const deadline = performance.now() + 15000;
                    while (video.currentTime <= 0.1 && !video.error && performance.now() < deadline)
                        await new Promise(resolve => setTimeout(resolve, 100));
                    const result = {advanced: video.currentTime > 0.1, currentTime: video.currentTime, width: video.videoWidth,
                                    height: video.videoHeight, error: video.error?.message || null};
                    video.pause(); return result;
                }''')
                assert result['advanced'] and result['width'] == 1920 and result['error'] is None, result
                check['browserPlayback'] = result
            for width in (1440, 390):
                await page.set_viewport_size({'width': width, 'height': 1000})
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await page.screenshot(path=str(OUTPUT / f'gallery-{width}.png'), full_page=True)
            assert not errors, errors
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()
    report = dict(passed=True, videoCount=len(checks), checks=checks, galleryWidths=[1440, 390],
                  note='Full codec decode, nonzero audio and actual browser playback verified; log-based audio timing is approximate.')
    (OUTPUT / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    OUTPUT = args.output.resolve()
    asyncio.run(main())
