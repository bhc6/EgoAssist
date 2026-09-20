"""Export uncut browser cases with actual response audio and audited gold reveal."""
import argparse
import html
import json
import re
import subprocess
import wave
from pathlib import Path
import av
import imageio_ffmpeg
import numpy as np
from build_story_videos import ROOT, load_source, sha, wall
from build_natural_story_videos import wav_samples
from run_egolife_benchmark_demo import OFFICIAL, public, prompt
from egoassist.eval.choice import explicit_choice

def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

def audit(source):
    folder=source['folder'];rows=[]
    for case in source['report']['cases']:
        if not case['passed']:
            continue
        assert case['recordingFinishedAt']
        qid=case['question']['ID'];step=case['steps'][0];score=case['comparison']
        official=next(r for r in json.loads(OFFICIAL.read_text('utf-8')) if r['ID']==qid)
        assert sha(OFFICIAL)==score['officialSha256']
        assert case['question']==public(official) and step['input']==prompt(public(official))
        assert score['goldChoice']==official['answer']
        prediction=json.loads((folder/f'prediction-{qid}.json').read_text('utf-8'))
        assert sha(folder/f'prediction-{qid}.json')==score['predictionSha256']
        assert prediction['answer']==step['answer']
        assert wall(prediction['savedAt'])<wall(score['revealedAt'])
        assert not any(k in case['question'] for k in ['answer','target_time','reason','reason_chinese'])
        frames={f['sha256']:f for f in case['frameFeed']}
        journal=json.loads((folder/'journals'/f'{case["id"]}.json').read_text('utf-8'))
        scene=next(r for r in reversed(journal) if r.get('type')=='scene.turn' and r.get('turnId')==step['turnId'])
        direct=scene.get('frame') or {}
        observation=scene.get('observationContext') or {}
        ref=(direct or observation.get('frame') or {}).get('sha256')
        assert ref in frames, 'Answer must have a frame from this question video window'
        answers=[e for e in source['events'] if e.get('type')=='assistant_transcript' and e.get('done') and e.get('origin',{}).get('turnId')==step['turnId']]
        assert any(e.get('text')==step['answer'] for e in answers)
        assert step['audio'] and step['turn']['source']=='explicit-control'
        assert step['turn']['text']==prediction['input']
        choice=explicit_choice(step['answer'])
        assert choice==score['predictedChoice'] and score['correct']==(choice==score['goldChoice'])
        rows.append({'qid':qid,'turnId':step['turnId'],'responseIds':step['responseIds'],
                     'framesSent':len(case['frameFeed']),'distinctFrames':len(frames),'answerFrame':frames[ref],
                     'visualAnswerPath':'current-frame' if direct else 'previous-model-observation',
                     'observationSourceId':observation.get('sourceId'),'visibleEvidence':observation.get('visibleEvidence'),
                     'predictionBeforeReveal':True,'inputHasNoGoldFields':True,'spokenAndWrittenAnswerMatch':True,
                     'comparison':score})
    result={'passed':True,'checks':rows,'scope':'Input transport, prediction immutability, audio correspondence and literal choice scoring; not visual correctness or full benchmark accuracy.'}
    write(folder/'benchmark-evidence-check.json',result)
    return result

def build(case,source,out):
    folder=source['folder'];origin=wall(case['recordingStartedAt']);start=origin-source['origin']
    duration=wall(case['recordingFinishedAt'])-origin
    # Align to a 25 FPS boundary for reproducible decode verification.
    duration=np.ceil(duration*25)/25
    mix=np.zeros(round(duration*24000),np.float64);audio=[]
    def add(path,t,kind,tid,rid=None):
        at=wall(t)-origin;samples=wav_samples(path);offset=round(at*24000)
        assert offset>=0 and offset+len(samples)<=len(mix)
        mix[offset:offset+len(samples)]+=samples
        audio.append({'file':str(path.relative_to(ROOT)),'sha256':sha(path),'startSeconds':at,
                      'durationSeconds':len(samples)/24000,'kind':kind,'turnId':tid,'responseId':rid})
    for step in case['steps']:
        add(folder/step['demonstratorAudio'],step['demonstratorAudioStartedAt'],'synthetic-readout-of-typed-question',step['turnId'])
        for filename in step['audio']:
            rid=next(r for r in step['responseIds'] if r.endswith(Path(filename).stem.split('-')[-1]))
            event=next(e for e in source['events'] if e.get('type')=='audio' and e.get('responseId')==rid)
            add(folder/filename,event['capturedAt'],'original-model-response',step['turnId'],rid)
    audio.sort(key=lambda x:x['startSeconds'])
    work=out/'edit-data';work.mkdir(exist_ok=True)
    wav=work/(case['id']+'.wav')
    with wave.open(str(wav),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(np.clip(mix,-32768,32767).astype('<i2').tobytes())
    target=out/(case['id']+'-benchmark.mp4')
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-y','-loglevel','error','-ss',str(start),'-i',str(source['video']),
                    '-i',str(wav),'-t',str(duration),'-vf','scale=-2:1080,pad=1920:1080:(ow-iw)/2:0:color=0x183c33,setsar=1,fps=25',
                    '-c:v','libx264','-preset','fast','-crf','21','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k',
                    '-map','0:v:0','-map','1:a:0','-movflags','+faststart',str(target)],check=True)
    poster=case['id']+'-benchmark.jpg'
    with av.open(str(target)) as c:
        c.seek(round((duration-3)*1e6));next(f for f in c.decode(video=0) if f.time>=duration-3).to_image().save(out/poster)
    return {'id':case['id'],'file':target.name,'poster':poster,'durationSeconds':duration,'sha256':sha(target),'sourceFolder':folder.name,
            'audio':audio,'question':case['question'],'answer':case['steps'][0]['answer'],'comparison':case['comparison'],
            'sourceRangeSeconds':[start,start+duration],'sourceVideo':str(source['video'].relative_to(ROOT)),
            'sourceVideoSha256':sha(source['video']),'scope':case['scope'],'playbackSpeed':1}

def gallery(out,entries,folder):
    esc=html.escape;cards=[]
    for e in entries:
        c=e['comparison'];q=e['question'];result='一致' if c['correct'] else ('未选择／无法确定' if c['predictedChoice'] is None else '不一致')
        ev=e['evidence']
        cards.append(f'''<article><h2>#{q['ID']} · {esc(q['question_chinese'])}</h2><p class="note">{esc(q['question'])}</p>
<video controls playsinline preload="metadata" src="{e['file']}" poster="{e['poster']}"></video>
<p>{e['durationSeconds']:.0f} 秒 · <a download href="{e['file']}">下载完整录屏</a> · <a href="../{e['sourceFolder']}/comparison-{q['ID']}.json">本题对比日志</a></p>
<table><tr><th>系统实际回答</th><th>Ground truth</th><th>字母对比</th></tr><tr><td>{esc(e['answer'])}</td><td>{c['goldChoice']}. {esc(c['goldOption'])}</td><td>{result}</td></tr></table>
<p>官方依据原文：{esc(c['reasonChinese'])}<br><span class="note">{esc(c['reason'])}</span></p>
<p class="note">{'本题实际回答根据外观和选项猜测姓名，缺少身份依据；字母与标准答案不一致。' if q['ID']=='81' else '本题只输入标注证据的一部分；对比结果同时受证据覆盖范围限制。'}</p>
<p class="note">{esc(e['scope'])}</p><details><summary>展开实际画面理解与回答链路</summary>
<p>{ev['framesSent']} 个视频帧 → 系统场景观察 → 官方原题 → 文字与语音回答 → 固定预测 → 标准答案字母对比。</p>
<p>本题回答引用之前生成的场景观察；回答时未关联新的当前帧。观察对应原视频 {ev['answerFrame']['sourceSeconds']:.2f} 秒，不能视为完整视频记忆。</p>
<p>实际场景观察原文：</p>{''.join('<p>'+esc(t)+'</p>' for t in (ev.get('visibleEvidence') or []))}
<pre>turnId: {ev['turnId']}\nobservationSourceId: {ev['observationSourceId']}\nframe SHA256: {ev['answerFrame']['sha256']}</pre>
<a href="../{e['sourceFolder']}/journals/{e['id']}.json">原始会话日志</a></details>
<details><summary>原始四选项与标注时刻</summary>{''.join('<p>'+x+'. '+esc(q['choice_'+x.lower()+'_chinese'])+' / '+esc(q['choice_'+x.lower()])+'</p>' for x in 'ABCD')}<pre>{esc(json.dumps(c['targetTime'],ensure_ascii=False,indent=2))}</pre></details></article>''')
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EgoLifeQA 原题 · 回答与 Ground truth</title>
<style>*{box-sizing:border-box}body{margin:0;background:#eef3f0;color:#183c33;font:17px/1.75 system-ui,'Microsoft YaHei',sans-serif}main{max-width:1320px;margin:auto;padding:30px}h1{font-size:34px}h2{font-size:25px}article{background:white;padding:24px;border-radius:16px;margin:26px 0}video{width:100%;aspect-ratio:16/9;background:#183c33}a{color:#12634c}nav{display:flex;gap:20px;flex-wrap:wrap}.note{font-size:14px;color:#586f65}table{width:100%;border-collapse:collapse;table-layout:fixed}td,th{text-align:left;vertical-align:top;border:1px solid #cbdad1;padding:12px;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer}@media(max-width:600px){main{padding:14px}article{padding:14px}h1{font-size:28px}table{font-size:13px}td,th{padding:7px}}</style>
<main><p>EGOASSIST × EGOLIFEQA / 原题实录</p><h1>先看视频，再作答，最后揭晓标准答案</h1>
<p>每段原速保留：一分钟视频输入 → 官方中文原题与四选项 → 系统实际文字与语音回答 → 回答落盘 → Ground truth 对照。</p>
<p class="note">依据本地素材覆盖范围选取两道原题，均在本轮运行前确定。使用标注时刻辅助截取视频，允许证据不足时不作选择；不是完整历史检索，也不是官方强制四选一评测。标准答案与官方解释仅用于作答后的展示和字母匹配，不送入模型。视频原声关闭；问题通过文字输入，合成音只作朗读。助手使用真实输出音频，按日志近似对齐。</p>'''
    page+=f'<nav><a href="../egolife-long-video-20260916/index.html">新增：约 12 分钟完整长录屏</a><a href="manifest.json">视频与日志索引</a><a href="benchmark-evidence-check.json">真实链路核验</a><a href="verification.json">播放检查</a><a href="../{folder.name}/protocol.json">输入范围</a></nav>'
    page+=''.join(cards)+'<p class="note">官方题目来源：<a href="https://huggingface.co/datasets/lmms-lab/EgoLife/blob/143fb319be7aa5ae210c936bf4f0f3a86092afb0/EgoLifeQA/EgoLifeQA_A1_JAKE.json">EgoLifeQA_A1_JAKE.json 固定版本</a>。原始文件 SHA256 与每轮预测 SHA256 均保存在对比日志中。第一轮录制的第二题在提交前遇到授权按钮等待超时；修复后仅补录第二题，没有按回答对错重试。</p></main></html>'
    (out/'index.html').write_text(page,encoding='utf-8')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,action='append',required=True);p.add_argument('--output',type=Path,default=ROOT/'outputs/egolife-benchmark-videos-20260916');a=p.parse_args()
    sources=[load_source(s.resolve()) for s in a.source];out=a.output.resolve();out.mkdir(exist_ok=True,parents=True)
    checks=[audit(s) for s in sources]
    cases=[(c,s) for s in sources for c in s['report']['cases'] if c['passed']]
    assert sorted(c['id'] for c,s in cases)==['q102','q81'], 'Exactly one completed prediction per preselected question'
    entries=[build(c,s,out) for c,s in cases]
    for e in entries:
        e['evidence']=next(row for run in checks for row in run['checks'] if row['qid']==e['question']['ID'])
    evidence={'passed':True,'runs':checks};write(out/'benchmark-evidence-check.json',evidence)
    write(out/'manifest.json',{'videos':entries,'evidenceAudit':evidence,'mock':False,'fullBenchmark':False,
                             'sourceReports':[{'path':str((s['folder']/'report.json').relative_to(ROOT)),'sha256':sha(s['folder']/'report.json'),'passed':s['report']['passed']} for s in sources]})
    gallery(out,entries,sources[0]['folder']);print(str(out/'index.html'),flush=True)
