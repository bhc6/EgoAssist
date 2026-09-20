"""Build long conversational videos from actual browser runs and response audio."""
from __future__ import annotations

import argparse
from datetime import datetime
import html
import json
from pathlib import Path
import subprocess
import wave
import zipfile

import av
import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw
from PIL import ImageFont

from build_story_videos import ROOT, FONT, load_source, sha, wall, wrapped


TITLES = {
    'elder': ('01-elder-conversation', '陪我记住家里的小事', '记住位置 → 改口纠正 → 回来接着聊'),
    'career': ('02-career-conversation', '把今天的忙碌，说成自己的经历', '聊工作 → 整理周报 → 不夸大成果'),
    'life': ('03-life-conversation', '出门前，你陪我检查一下', '找齐东西 → 中途离开 → 接着确认装包'),
    'access': ('04-access-conversation', '你慢慢说，我跟着听', '先听概览 → 追问细节 → 换成语音提问'),
}


def draw_panel(case, number, question, answer, status, final=False, picture=None):
    im = Image.new('RGBA', (1920, 1080), '#eff3f0')
    draw = ImageDraw.Draw(im)
    im.paste((0, 0, 0, 0), (32, 160, 1112, 910))
    _, title, journey = TITLES[case['id']]
    wrapped(draw, title, 32, 27, 1550, 44, '#163c35', bold=True)
    wrapped(draw, journey, 35, 96, 1520, 24, '#55736b')
    wrapped(draw, 'EgoAssist', 1630, 40, 260, 30, '#226652', bold=True)
    wrapped(draw, '实际浏览器操作 · 原速', 32, 130, 1000, 18, '#55736b')
    x, width = 1150, 730
    total = len(case['steps'])
    wrapped(draw, f'{number:02d} / {total:02d}  轮对话' if not final else f'{total} 轮完整对话 · 结束回看', x, 147, width, 23, '#55736b')
    draw.rounded_rectangle((x, 199, 1888, 403), radius=18, fill='#dcebe4')
    wrapped(draw, '演示者', x + 22, 216, width - 44, 20, '#276652', bold=True)
    q_end = wrapped(draw, question, x + 22, 256, width - 44, 29, '#183a32', spacing=10)
    if q_end > 400:
        raise ValueError(f'Question exceeds panel: {q_end}')
    draw.rounded_rectangle((x, 421, 1888, 957), radius=18, fill='#ffffff')
    wrapped(draw, 'EgoAssist', x + 22, 440, width - 44, 22, '#276652', bold=True)
    wrapped(draw, status, x + 200, 445, width - 224, 17, '#6d8178')
    answer_size = 29
    for size in (29, 27, 25, 23, 21):
        font = ImageFont.truetype(FONT, size)
        count, line = 1, ''
        for char in answer:
            if char == '\n' or draw.textlength(line + char, font=font) > width - 44:
                count += 1
                line = '' if char == '\n' else char
            else:
                line += char
        answer_size = size
        if count * (size + 12) <= 458:
            break
    end = wrapped(draw, answer or '…', x + 22, 487, width - 44, answer_size, '#172d27', spacing=12)
    if end > 949:
        raise ValueError(f'Answer exceeds panel: {end}')
    if picture is not None and end < 650:
        with Image.open(picture) as source:
            thumbnail = source.convert('RGBA')
            thumbnail.thumbnail((440, 242))
            wrapped(draw, '模型收到的原图 · 后期展示', x + 22, 665, width - 44, 17, '#6d8178')
            im.alpha_composite(thumbnail, (x + 22, 701))
    wrapped(draw, '真实系统录屏与日志 · 2026-09-16', 32, 940, 1080, 22, '#345c4c')
    wrapped(draw, '双方完整来回，保留实际等待；画面旁的对话文字来自本轮输入与原始回答。', 32, 978, 1080, 21, '#55736b')
    draw.line((32, 1027, 1888, 1027), fill='#c5d8cd', width=2)
    wrapped(draw, '演示者为合成语音；助手使用真实回答音频；保存回执另行标注原文朗读。', 32, 1040, 1850, 18, '#55736b')
    return im


def wav_samples(path):
    with av.open(str(path)) as source:
        resampler = av.AudioResampler(format='s16', layout='mono', rate=24000)
        samples = []
        for frame in source.decode(audio=0):
            samples.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(frame))
        samples.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))
    return np.concatenate(samples)


def build(case, source, output):
    stem, title, journey = TITLES[case['id']]
    folder = source['folder']
    cache = output / (case['id'] + '-export.json')
    if cache.exists():
        entry = json.loads(cache.read_text(encoding='utf-8'))
        target = output / entry['file']
        if (entry.get('sourceReportSha256') == sha(folder / 'report.json')
                and target.exists() and entry['sha256'] == sha(target)):
            print(f'Using verified export cache: {target.name}', flush=True)
            return entry
    start = wall(case['recordingStartedAt']) - source['origin']
    end = wall(case['recordingFinishedAt']) - source['origin']
    runtime, duration = end - start, end - start + 4
    origin = source['origin'] + start
    work = output / 'edit-data' / case['id']
    work.mkdir(parents=True, exist_ok=True)
    mix = np.zeros(round(duration * 24000), np.float64)
    clips, cues = [], []

    def add_audio(path, at, kind, turn_id, response_id=None):
        samples = wav_samples(path)
        offset = round(at * 24000)
        if offset < 0 or offset + len(samples) > len(mix):
            raise ValueError(f'Audio out of bounds {path}: {at}')
        mix[offset:offset + len(samples)] += samples
        clips.append(dict(file=str(path.relative_to(ROOT)), startSeconds=round(at, 3),
                          durationSeconds=len(samples) / 24000, kind=kind, turnId=turn_id,
                          responseId=response_id, sha256=sha(path)))

    for number, step in enumerate(case['steps'], 1):
        at = wall(step['demonstratorAudioStartedAt']) - origin
        add_audio(folder / step['demonstratorAudio'], at, 'synthetic-demonstrator', step['turnId'])
        cues.append((at, number, step['input'], '', '正在听你说', False))
        answer_at = wall(step['turn']['updatedAt']) - origin
        status = '真实模型回答'
        for filename in step['audio']:
            rid = next(r for r in step['responseIds'] if r.endswith(Path(filename).stem.split('-')[-1]))
            event = next(e for e in source['events'] if e.get('type') == 'audio' and e.get('responseId') == rid)
            pos = wall(event['capturedAt']) - origin
            answer_at = min(answer_at, pos)
            add_audio(folder / filename, pos, 'original-model-response', step['turnId'], rid)
        if step.get('receiptReadout'):
            receipt = step['receiptReadout']
            pos = wall(receipt['startedAt']) - origin
            add_audio(folder / receipt['file'], pos, 'verbatim-server-receipt-tts', step['turnId'])
            status = '保存回执 · 原文合成朗读'
        cues.append((answer_at, number, step['input'], step['answer'], status, False))
    cues.sort(key=lambda c: c[0])
    cues[0] = (0, *cues[0][1:])
    last = case['steps'][-1]
    cues.append((runtime, len(case['steps']), last['input'], last['answer'], '最后一轮实际回答', True))
    lines = []
    picture = folder / 'egolife-input.jpg' if case['id'] == 'access' else None
    for i, cue in enumerate(cues):
        draw_panel(case, *cue[1:], picture=picture).save(work / f'panel-{i:03d}.png')
        stop = cues[i + 1][0] if i + 1 < len(cues) else duration
        lines += [f"file 'panel-{i:03d}.png'", f'duration {stop - cue[0]:.6f}']
    lines += [f"file 'panel-{len(cues)-1:03d}.png'"]
    (work / 'panels.ffconcat').write_text('\n'.join(lines), encoding='utf-8')
    with wave.open(str(work / 'mixed.wav'), 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        audio.writeframes(np.clip(mix, -32768, 32767).astype('<i2').tobytes())
    target = output / (stem + '.mp4')
    filters = (f'[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS,fps=25,scale=1080:750,'
               'pad=1920:1080:32:160:color=0xeff3f0,tpad=stop_mode=clone:stop_duration=4[base];'
               '[base][1:v]overlay=0:0:eof_action=repeat:format=auto,format=yuv420p[out]')
    command = [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'warning', '-y',
               '-i', str(source['video']), '-f', 'concat', '-safe', '0', '-i', str(work / 'panels.ffconcat'),
               '-i', str(work / 'mixed.wav'), '-filter_complex', filters, '-map', '[out]', '-map', '2:a',
               '-t', str(duration), '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-threads', '4',
               '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(target)]
    print(f'Encoding {target.name}: {duration:.1f}s / {len(case["steps"])} turns', flush=True)
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    with av.open(str(target)) as container:
        container.seek(round((duration - 2) * 1e6))
        frame = next(f for f in container.decode(video=0) if f.time >= duration - 2)
        frame.to_image().save(output / (stem + '.jpg'), quality=92)
    entry = dict(id=case['id'], title=title, journey=journey, file=target.name, poster=stem + '.jpg',
                turns=case['steps'], turnCount=len(case['steps']), durationSeconds=round(duration, 2),
                audio=clips, sourceVideo=str(source['video'].relative_to(ROOT)), sourceVideoSha256=sha(source['video']),
                sourceRangeSeconds=[start, end], sourceReport=str((folder / 'report.json').relative_to(ROOT)),
                sourceReportSha256=sha(folder / 'report.json'), sha256=sha(target),
                note=case['scope'], playbackSpeed=1, endingHoldSeconds=4)
    cache.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding='utf-8')
    return entry


def gallery(output, entries):
    cards = []
    for e in entries:
        esc = html.escape
        transcript = ''.join(f'<p><b>演示者：</b>{esc(s["input"])}</p><p class="answer"><b>EgoAssist：</b>{esc(s["answer"])}</p>' for s in e['turns'])
        cards.append(f'''<article><h2>{esc(e['title'])}</h2><p>{esc(e['journey'])}</p>
<video controls playsinline preload="metadata" poster="{e['poster']}" src="{e['file']}"></video>
<p>{e['turnCount']} 轮对话 · {e['durationSeconds']/60:.1f} 分钟 · <a download href="{e['file']}">下载视频</a></p>
<p class="note">{esc(e['note'])}</p><details><summary>展开完整对话原文</summary>{transcript}</details></article>''')
    content = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EgoAssist · 生活里的长对话</title>
<style>*{box-sizing:border-box}body{margin:0;background:#eff3f0;color:#183a32;font:17px/1.75 system-ui,"Microsoft YaHei",sans-serif}header,main,footer{max-width:1260px;padding:28px;margin:auto}h1{font-size:38px;margin:10px 0}h2{font-size:27px;margin:0}article{background:white;padding:24px;border-radius:18px;margin:0 0 32px}video{display:block;width:100%;aspect-ratio:16/9;background:#163c35;border-radius:10px}a{color:#13624c}.note{font-size:14px;color:#526d62}.answer{border-left:3px solid #a6cdb9;padding-left:16px}summary{cursor:pointer}nav{display:flex;gap:20px;flex-wrap:wrap}details{border-top:1px solid #dce7df;padding-top:12px}@media(max-width:600px){header,main,footer{padding:16px}h1{font-size:30px}h2{font-size:23px}article{padding:16px}}</style>
<header><p>EGOASSIST / 真实系统，多轮对话</p><h1>像平时一样，慢慢聊下去</h1><p>四个场景，每段 8–10 轮。会补充、会改口，会中途离开，也会回来接着说。</p>
<p class="note">2026-09-16 重新实际运行。演示者台词由合成语音读出，通常通过文字输入；无障碍片段第 7 轮经过真实 ASR。助手使用本轮模型原始声音，文字保存回执另标注合成朗读。音轨按日志时间近似对齐，保持实际操作速度。情境演示尚非真实用户研究。</p>
<nav><a href="../egolife-long-video-20260916/index.html">新增：约 12 分钟完整长录屏</a><a href="../egolife-benchmark-videos-20260916/index.html">新增：Benchmark 原题与 Ground truth 对照</a><a href="../egolife-multimodal-video-20260916/index.html">新增：EgoLife 连续视频与语音实录</a><a download href="natural-dialogue-videos.zip">打包下载四段视频</a><a href="manifest.json">原始链路索引</a><a href="verification.json">播放检查结果</a></nav></header><main>'''
    content += ''.join(cards) + '</main><footer>原始失败运行保留在 natural-story-20260916-r1；修复记录见项目演示文档。</footer></html>'
    (output / 'index.html').write_text(content, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, action='append', required=True,
                        help='May repeat; later completed cases override earlier cases with the same id.')
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/natural-story-videos-20260916')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    chosen = {}
    for path in args.source:
        source = load_source(path.resolve())
        for case in source['report']['cases']:
            if case.get('passed') and case.get('recordingFinishedAt'):
                chosen[case['id']] = (case, source)
    if set(chosen) != set(TITLES):
        raise ValueError(f'Missing validated cases: {set(TITLES) - set(chosen)}')
    entries = [build(*chosen[case_id], output) for case_id in TITLES]
    manifest = dict(createdAt=datetime.now().astimezone().isoformat(), videos=entries,
                    mock=False, fieldStudy=False, audioAlignment='Approximate log alignment; synthetic demonstrator and receipt voice marked separately.')
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    gallery(output, entries)
    with zipfile.ZipFile(output / 'natural-dialogue-videos.zip', 'w', compression=zipfile.ZIP_STORED) as archive:
        for entry in entries:
            archive.write(output / entry['file'], entry['file'])
        archive.write(output / 'manifest.json', 'manifest.json')
    print(f'Complete: {output / "index.html"}', flush=True)


if __name__ == '__main__':
    main()
