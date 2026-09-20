"""Edit retained real browser recordings, restoring their own provider audio.

Requires Pillow, numpy, av and imageio-ffmpeg. No model calls or synthetic outputs.
The source recordings and evidence files are never modified.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path
import subprocess
import wave
import zipfile

import av
import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONT = 'C:/Windows/Fonts/msyh.ttc'
BOLD = 'C:/Windows/Fonts/msyhbd.ttc'
CASES = [
    dict(id='elder', file='01-elder-memory', title='老人：记住物品，也能纠正记忆', start=.35, end=15.78,
         points=['保存备用钥匙的位置', '纠正：绿色收纳盒 → 黄色抽屉', '刷新后，回答使用更新后的记忆'],
         note='老人使用情境演示；尚未进行老人真实用户研究。'),
    dict(id='career', file='02-career-record', title='职业记录：留下可追溯的工作经历', start=15.84, end=29.10,
         points=['保存项目测试的工作记录', '准确回答：用户研究尚未进行', '生成带引用的当日日报'],
         note='展示工作记录与日报；不等同于已验证完整职业规划能力。'),
    dict(id='life', file='03-life-checklist', title='生活助手：把出门检查接着做完', start=29.17, end=85.70,
         points=['逐步核对钥匙、手机、门禁卡', '暂停 → 刷新 → 恢复原任务', '收到携带确认后完成任务'],
         note='完成状态来自用户确认；不表示摄像头已验证实物装包。'),
    dict(id='access', file='04-accessibility', title='无障碍：把画面转成语音', start=85.80, end=124.22,
         points=['描述实际 EgoLife 画面并朗读', '语音输入经过真实 ASR 转写', '“七乘八”得到回答“56”'],
         note='测试输入语音由 TTS 合成；尚未进行残障人士真实用户研究。'),
    dict(id='handoff', file='05-live-camera', title='现场设备：跨会话回忆与摄像头', start=.35, end=22.72,
         points=['服务重启、新会话仍能回忆钥匙', '接入真实电脑摄像头', '镜头被遮挡时，如实回答画面很暗'],
         note='本段麦克风关闭、摄像头开启；未展示真人麦克风问答。'),
]


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wall(value):
    return datetime.fromisoformat(value).timestamp()


def wrapped(draw, text, x, y, width, size=25, color='#e7edf3', bold=False, spacing=11):
    font = ImageFont.truetype(BOLD if bold else FONT, size)
    line = ''
    for char in text + '\n':
        if char == '\n' or draw.textlength(line + char, font=font) > width:
            draw.text((x, y), line, font=font, fill=color)
            y += size + spacing
            line = '' if char == '\n' else char
        else:
            line += char
    return y


def panel(case, label, input_text='', answer='', turn_id='', final=False):
    im = Image.new('RGBA', (1920, 1080), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 1919, 79), fill='#102632')
    d.rectangle((1440, 80, 1919, 1079), fill='#102632')
    wrapped(d, case['title'], 30, 18, 1360, 31, bold=True)
    wrapped(d, 'EgoAssist  /  真实链路演示', 1470, 25, 420, 21, '#84d9c4')
    wrapped(d, '结果回看 · 末帧停留' if final else '原速录屏 · 2026-09-15', 1470, 102, 420, 18, '#9db2bd')
    y = wrapped(d, label, 1470, 150, 420, 31, bold=True) + 22
    if final:
        for number, point in enumerate(case['points'], 1):
            y = wrapped(d, f'{number}. ' + point, 1470, y, 420, 27, '#b6efde') + 25
        wrapped(d, '对应原始日志与响应音频可在视频目录的证据索引中核对。', 1470, y + 15, 420, 23, '#c4d4de')
    else:
        y = wrapped(d, '演示输入', 1470, y, 420, 18, '#84d9c4') + 9
        y = wrapped(d, input_text, 1470, y, 420, 24) + 22
        y = wrapped(d, '系统实际回答' if answer else '系统状态', 1470, y, 420, 18, '#84d9c4') + 9
        y = wrapped(d, answer or '等待响应…', 1470, y, 420, 24, '#ffffff')
        if y > 860:
            raise ValueError(f'Panel text overflow: {case["id"]} {label}: {y}')
        if turn_id:
            wrapped(d, 'turnId · ' + turn_id, 1470, 874, 420, 15, '#92aab6', spacing=6)
    if case['id'] == 'access' and (final or (not answer and '01' in label)):
        wrapped(d, '本轮真实输入图片 · 后期展示', 1470, 555, 420, 19, '#84d9c4')
        with Image.open(ROOT / 'outputs/story-showcase-20260915-r4/egolife-input.jpg') as source:
            reference = source.convert('RGBA')
            reference.thumbnail((420, 264))
            im.alpha_composite(reference, (1470, 590))
    d.line((1470, 928, 1890, 928), fill='#35505e', width=2)
    wrapped(d, case['note'], 1470, 943, 420, 19, '#b7c8d1', spacing=8)
    wrapped(d, '原始回答音轨 · 按日志时间还原', 1470, 1041, 420, 16, '#84d9c4')
    return im


def load_source(folder):
    with zipfile.ZipFile(folder / 'playwright-trace.zip') as archive:
        rows = [json.loads(line) for line in archive.read('trace.trace').splitlines()]
    frame = next(r for r in rows if r['type'] == 'screencast-frame')
    events = [json.loads(line) for line in (folder / 'browser-events.jsonl').read_text(encoding='utf-8').splitlines()]
    # Trace/video clocks have frame-level uncertainty. This is a log-aligned remix,
    # not a claim that the browser's system audio was recorded.
    return dict(folder=folder, report=read(folder / 'report.json'), events=events,
                origin=frame['frameSwapWallTime'] / 1000, mono=frame['timestamp'] / 1000,
                rows=rows, video=next((folder / 'recordings').glob('*.webm')))


def build(case, source, output):
    work = output / 'edit-data' / case['id']
    work.mkdir(parents=True, exist_ok=True)
    original = next(c for c in source['report']['cases'] if c['id'] == case['id'])
    runtime = case['end'] - case['start']
    duration = runtime + 5
    origin = source['origin'] + case['start']
    audio_mix = np.zeros(round(duration * 24000), dtype=np.float64)
    cues, audio_manifest, turns = [], [], []
    for number, step in enumerate(original['steps'], 1):
        turn = step['turn']
        begin = max(0, wall(turn['timestamp']) - origin)
        label = f'{number:02d}  {step["label"]}'
        cues.append((begin, label, step['input'], '', step['turnId'], False))
        first_audio = None
        for filename in step.get('audio', []):
            rid = next(r for r in step['responseIds'] if r.endswith(Path(filename).stem.split('-')[-1]))
            event = next(e for e in source['events'] if e.get('type') == 'audio' and e.get('responseId') == rid)
            position = wall(event['capturedAt']) - origin
            first_audio = position if first_audio is None else min(first_audio, position)
            audio_path = source['folder'] / filename
            with wave.open(str(audio_path), 'rb') as wav:
                assert wav.getframerate() == 24000 and wav.getnchannels() == 1 and wav.getsampwidth() == 2
                samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
            offset = round(position * 24000)
            if offset < 0 or offset + len(samples) > len(audio_mix):
                raise ValueError(f'Audio outside clip: {filename}')
            audio_mix[offset:offset + len(samples)] += samples
            audio_manifest.append(dict(file=str(audio_path.relative_to(ROOT)), sha256=sha(audio_path),
                                       responseId=rid, turnId=step['turnId'], startSeconds=round(position, 3),
                                       durationSeconds=len(samples) / 24000))
        answer_at = first_audio if first_audio is not None else wall(turn.get('updatedAt', turn['timestamp'])) - origin
        cues.append((max(begin, answer_at), label, step['input'], step['answer'], step['turnId'], False))
        turns.append(dict(turnId=step['turnId'], label=step['label'], input=step['input'], answer=step['answer']))
    # Show the first intended interaction immediately; retain its actual action in the video.
    cues.sort(key=lambda c: c[0])
    cues[0] = (0, *cues[0][1:])
    cues = [c for c in cues if c[0] < runtime]
    cues.append((runtime, '本段已验证', '', '', '', True))
    lines = []
    for i, cue in enumerate(cues):
        path = work / f'panel-{i:02d}.png'
        panel(case, *cue[1:]).save(path)
        stop = cues[i + 1][0] if i + 1 < len(cues) else duration
        lines += [f"file '{path.name}'", f'duration {stop - cue[0]:.6f}']
    lines.append(f"file 'panel-{len(cues)-1:02d}.png'")
    (work / 'panels.ffconcat').write_text('\n'.join(lines), encoding='utf-8')
    with wave.open(str(work / 'restored-audio.wav'), 'wb') as wav:
        wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        wav.writeframes(np.clip(audio_mix, -32768, 32767).astype('<i2').tobytes())
    target = output / (case['file'] + '.mp4')
    filters = (f'[0:v]trim=start={case["start"]}:end={case["end"]},setpts=PTS-STARTPTS,fps=25,'
               'pad=1920:1080:0:80:color=0x102632,tpad=stop_mode=clone:stop_duration=5[base];'
               '[base][1:v]overlay=0:0:eof_action=repeat:format=auto,format=yuv420p[out]')
    command = [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'warning', '-y',
               '-i', str(source['video']), '-f', 'concat', '-safe', '0', '-i', str(work / 'panels.ffconcat'),
               '-i', str(work / 'restored-audio.wav'), '-filter_complex', filters, '-map', '[out]', '-map', '2:a',
               '-t', str(duration), '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-threads', '4',
               '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(target)]
    print(f'Encoding {target.name} ({duration:.1f}s)', flush=True)
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    with av.open(str(target)) as container:
        container.seek(round((duration - 2) * 1e6))
        poster = next(frame for frame in container.decode(video=0) if frame.time >= duration - 2).to_image()
        poster.save(output / (case['file'] + '.jpg'), quality=92)
    return dict(id=case['id'], title=case['title'], file=target.name,
                poster=case['file'] + '.jpg', durationSeconds=round(duration, 2), bytes=target.stat().st_size,
                sha256=sha(target), sourceVideo=str(source['video'].relative_to(ROOT)),
                sourceVideoSha256=sha(source['video']), sourceRangeSeconds=[case['start'], case['end']],
                finalFrameHoldSeconds=5, playbackSpeed=1, audioAlignment='First audio packet wall clock relative to first screencast frame; approximate, not system audio capture.',
                sourceReport=str((source['folder'] / 'report.json').relative_to(ROOT)),
                sourceReportSha256=sha(source['folder'] / 'report.json'), audio=audio_manifest, turns=turns,
                note=case['note'], points=case['points'])


def gallery(output, entries):
    esc = html.escape
    cards = []
    for e in entries:
        points = ''.join(f'<li>{esc(p)}</li>' for p in e['points'])
        cards.append(f'''<article id="{e['id']}"><h2>{esc(e['title'])}</h2>
<video controls playsinline preload="metadata" poster="{e['poster']}" src="{e['file']}"></video>
<p class="meta">{e['durationSeconds']:.1f} 秒 · 1920 × 1080 · 原速录屏 + 5 秒结果停留</p>
<ul>{points}</ul><p class="note">{esc(e['note'])}</p>
<a download href="{e['file']}">下载 MP4</a> · <a href="../../{e['sourceReport']}">原始案例日志</a></article>''')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EgoAssist · 五段真实演示</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f6f7;color:#18323d;font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif}
header,main,footer{max-width:1400px;margin:auto;padding:24px}header{padding-top:40px}h1{font-size:34px;margin:0}h2{font-size:23px;margin:0 0 16px}
main{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px}article{background:white;border:1px solid #dae3e7;border-radius:12px;padding:20px;min-width:0}
video{display:block;width:100%;aspect-ratio:16/9;background:#102632;border-radius:6px}a{color:#086853}nav{display:flex;gap:16px;flex-wrap:wrap}.meta{font-size:13px;color:#5d737d}.note{color:#576b75;font-size:14px}
@media(max-width:800px){main{grid-template-columns:1fr;padding:16px}header{padding:24px 16px}h1{font-size:28px}article{padding:16px}}
</style><header><p>EGOASSIST / 实际运行证据</p><h1>五个场景，五段可播放的演示</h1>
<p>来自 2026-09-15 的真实浏览器与云端模型运行。每段保留原速操作，配上中文步骤和实际回答原文。</p>
<p class="note">原录屏无系统音轨；这里把同一响应的原始 WAV 按日志时间近似对齐，恢复模型回答声音。右侧说明为后期添加；末尾停留 5 秒方便阅读。情境演示不等同于真实用户研究。</p>
<nav><a href="story-demo-videos.zip" download>下载全部视频 ZIP</a><a href="manifest.json">视频与链路证据索引</a><a href="verification.json">播放检查结果</a></nav></header><main>'''
    page += ''.join(cards) + '</main><footer>画面没有倍速播放。麦克风真人问答不在这五段视频的验证范围内。</footer></html>'
    (output / 'index.html').write_text(page, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/story-videos-20260916')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    main_source = load_source(ROOT / 'outputs/story-showcase-20260915-r4')
    live_source = load_source(ROOT / 'outputs/story-handoff-20260915-r4')
    entries = [build(c, live_source if c['id'] == 'handoff' else main_source, output) for c in CASES]
    manifest = dict(createdAt=datetime.now().astimezone().isoformat(), videos=entries, mock=False, fieldStudy=False)
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    gallery(output, entries)
    with zipfile.ZipFile(output / 'story-demo-videos.zip', 'w', compression=zipfile.ZIP_STORED) as archive:
        for entry in entries:
            archive.write(output / entry['file'], entry['file'])
        archive.write(output / 'manifest.json', 'manifest.json')
    print(f'Complete: {output / "index.html"}', flush=True)


if __name__ == '__main__':
    main()
