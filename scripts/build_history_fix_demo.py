"""Publish the actual browser replay and verify historical evidence delivery."""
import argparse
import html
import json
from pathlib import Path

from build_egolife_benchmark_videos import audit, build, write
from build_story_videos import load_source
from egoassist.eval.choice import explicit_choice


def main(args):
    source = load_source(args.source.resolve())
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    transport = audit(source)
    entries, checks = [], []
    for case in source['report']['cases']:
        assert case['passed']
        rows = json.loads((source['folder']/'journals'/f"{case['id']}.json").read_text('utf-8'))
        turn_id = case['steps'][0]['turnId']
        history = next(r for r in rows if r['type'] == 'scene.history.result' and r['turnId'] == turn_id)
        assert history['status'] == 'observed' and history['requestId']
        assert history['question'] == case['question']['question_chinese']
        jobs = [r for r in rows if r['type'] == 'answer.job' and r.get('turnId') == turn_id]
        assert jobs and all(r['context']['sharedScene']['historicalVisual']['requestId'] == history['requestId'] for r in jobs)
        source_ids = {s['sourceId'] for s in history['sources']}
        observations = {r['id']: r for r in rows if r['type'] == 'ambient_observation' and r['status'] == 'accepted'}
        assert source_ids <= observations.keys()
        frame_hashes = {f['sha256'] for f in case['frameFeed']}
        assert all(s['frame']['sha256'] in frame_hashes for s in history['sources'])
        entry = build(case, source, out)
        entries.append(entry)
        checks.append({'turnId': turn_id, 'historyProviderRequestId': history['requestId'],
                       'rankingQuery': history['question'], 'sourceCount': len(source_ids),
                       'sourcesMatchCapturedVideo': True, 'historyDeliveredToAnswer': True,
                       'historyLatencyMs': history['latencyMs'], 'choiceMatch': case['comparison']['correct']})
    rescored = []
    for folder in sorted(args.source.resolve().parent.glob('visual-history-real-20260916-*')):
        for path in sorted(folder.glob('prediction-*.json')):
            prediction = json.loads(path.read_text('utf-8'))
            old = json.loads((folder/'report.json').read_text('utf-8'))
            score = next((r for r in old['results'] if r['qid'] == prediction['qid']), None)
            rescored.append({'path': str(path), 'qid': prediction['qid'], 'choice': explicit_choice(prediction['answer']),
                             'gold': score['gold'] if score else None,
                             'originalCorrect': score['correct'] if score else None,
                             'strictChoiceMatch': bool(score and explicit_choice(prediction['answer']) == score['gold']),
                             'visualGroundingVerified': False})
    write(out/'manifest.json', {'videos': entries, 'mock': False, 'fullBenchmark': False})
    write(out/'architecture-check.json', {'passed': True, 'checks': checks, 'transportAudit': transport,
          'scope': 'Actual history provider call, exact question projection, captured-media provenance and delivery to answer; not semantic accuracy.'})
    write(out/'replay-rescoring.json', {'originalFilesPreserved': True, 'results': rescored,
          'note': 'Choice match alone does not validate visual reasoning; early B answers had unsupported descriptions.'})
    blocks = []
    for entry in entries:
        score = entry['comparison']
        blocks.append(f'<h2>原题 #{entry["question"]["ID"]}</h2><p>{html.escape(entry["question"]["question_chinese"])}</p>'
          f'<video controls playsinline preload="metadata" src="{entry["file"]}" poster="{entry["poster"]}"></video>'
          f'<p>原速 {entry["durationSeconds"]:.0f} 秒，保留输入、等待、回答及标准答案揭晓。</p>'
          f'<h3>系统实际回答</h3><p>{html.escape(entry["answer"])}</p>'
          f'<h3>Ground truth</h3><p>{score["goldChoice"]}. {html.escape(score["goldOption"])}。'
          f'字母匹配：{"通过" if score["correct"] else "未通过"}。</p>'
          f'<p>官方解释：{html.escape(score["reasonChinese"])}</p>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EgoAssist 历史视觉修复实测</title><style>*{box-sizing:border-box}body{margin:0;background:#f6f7f8;color:#20282d;font:17px/1.7 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1140px;margin:auto;padding:28px}h1{font-size:30px}h2{font-size:23px;margin-top:30px}h3{font-size:18px}a{color:#075d8e}video{width:100%;aspect-ratio:16/9;background:#111}nav{display:flex;gap:18px;flex-wrap:wrap}p{overflow-wrap:anywhere}@media(max-width:600px){main{padding:16px}h1{font-size:26px}}</style>
<main><h1>EgoAssist 历史视觉修复实测</h1>
<p>历史图像检索、视觉复查和回答传递已在真实浏览器验证；这不表示 benchmark 已答对。姓名题仍缺身份依据，替代物题仍有物体识别错误。</p>
<nav><a href="architecture-check.json">真实链路核验</a><a href="replay-rescoring.json">全部组件重跑与重评分</a><a href="verification.json">录屏播放检查</a><a href="http://127.0.0.1:8778">现场系统</a></nav>
<p>输入为 EgoLife 原始画面。该浏览器实录仅覆盖一分钟，原始环境音未输入；问题经文字提交，朗读为合成音，回答使用该轮真实输出音频。补充视频的组件诊断另存日志，不混作这段录屏的输入。</p>'''
    (out/'index.html').write_text(page + ''.join(blocks) + '</main></html>', encoding='utf-8')
    print(str(out/'index.html'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args())
