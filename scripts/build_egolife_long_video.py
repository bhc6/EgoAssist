"""Export one uncut recording covering both complete benchmark sessions."""
import argparse
import html
import json
from pathlib import Path
from build_egolife_benchmark_videos import audit, build, write
from build_story_videos import ROOT, load_source, sha, wall


def clock(seconds):
    seconds=int(seconds)
    return f'{seconds//60:02d}:{seconds%60:02d}'


def main(args):
    source=load_source(args.source.resolve());report=source['report']
    assert report['passed'] and len(report['cases'])==2
    cases=report['cases']
    assert [c['question']['ID'] for c in cases]==['81','102']
    protocol=json.loads((source['folder']/'protocol.json').read_text('utf-8'))
    assert protocol['videoSelection']=='all local video, same full range for each question'
    checks=audit(source)
    for case in cases:
        feed=case['frameFeed'];window=case['videoWindow']
        assert window['startSeconds']==0 and window['endSeconds']>280
        assert feed[0]['sourceSeconds']<2 and feed[-1]['sourceSeconds']>window['endSeconds']-2
        assert len(feed)>window['endSeconds']*1.9
    combined={'id':'egolife-long','question':{},'comparison':{},
              'recordingStartedAt':cases[0]['recordingStartedAt'],
              'recordingFinishedAt':cases[-1]['recordingFinishedAt'],
              'steps':[s for c in cases for s in c['steps']],
              'scope':'Single uncut browser recording, two independent sessions, all ten local video clips replayed at original speed for each question. No full-day history or original audio.'}
    out=args.output.resolve();out.mkdir(exist_ok=True,parents=True)
    entry=build(combined,source,out)
    assert entry['durationSeconds']>600
    origin=wall(combined['recordingStartedAt']);chapters=[]
    for case in cases:
        qid=case['question']['ID'];step=case['steps'][0]
        for label,at in [('完整视频输入',case['recordingStartedAt']),('原题朗读与提交',step['demonstratorAudioStartedAt']),
                         ('Ground truth 对比',case['comparison']['revealedAt'])]:
            chapters.append({'title':f'#{qid} · {label}','startSeconds':wall(at)-origin})
    entry.update(chapters=chapters,comparisons=[c['comparison'] for c in cases],uncut=True,
                 framesSent=sum(len(c['frameFeed']) for c in cases),evidenceAudit=checks)
    write(out/'manifest.json',{'videos':[entry],'mock':False,'fullBenchmark':False,'uncut':True,
                             'sourceReport':str((source['folder']/'report.json').relative_to(ROOT)),
                             'sourceReportSha256':sha(source['folder']/'report.json')})
    write(out/'benchmark-evidence-check.json',checks)
    esc=html.escape
    buttons=''.join(f'<button onclick="seekTo({c["startSeconds"]:.3f})">{clock(c["startSeconds"])}　{esc(c["title"])}</button>' for c in chapters)
    rows=''.join(f'<tr><td>#{c["question"]["ID"]}<br>{esc(c["question"]["question_chinese"])}</td><td>{esc(c["steps"][0]["answer"])}</td><td>{c["comparison"]["goldChoice"]}. {esc(c["comparison"]["goldOption"])}</td><td>{"一致" if c["comparison"]["correct"] else "不一致／未选择"}</td></tr>' for c in cases)
    page=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EgoLifeQA · 完整长录屏</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#eef3f0;color:#183c33;font:17px/1.75 system-ui,'Microsoft YaHei',sans-serif}}main{{max-width:1360px;margin:auto;padding:30px}}h1{{font-size:36px;margin:10px 0}}video{{width:100%;aspect-ratio:16/9;background:#183c33;border-radius:12px}}a{{color:#12634c}}nav,.chapters{{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}}button{{font:inherit;background:white;color:#183c33;border:1px solid #b6cabe;border-radius:8px;padding:10px 14px;cursor:pointer}}.note{{font-size:14px;color:#567063}}details{{margin-top:28px;background:white;border-radius:12px;padding:20px}}summary{{cursor:pointer}}table{{border-collapse:collapse;width:100%;table-layout:fixed;margin-top:20px}}td,th{{text-align:left;vertical-align:top;border:1px solid #c9d9cd;padding:12px;overflow-wrap:anywhere}}td:nth-child(2),th:nth-child(2){{width:43%}}@media(max-width:600px){{main{{padding:14px}}h1{{font-size:28px}}table{{font-size:13px}}td,th{{padding:6px}}button{{font-size:14px}}}}</style>
<main><p>EGOASSIST × EGOLIFEQA / 连续实录</p><h1>完整视频输入，直到答案揭晓</h1>
<p>一条 {clock(entry['durationSeconds'])} 长录屏。两道官方原题，每题独立播放全部 10 个本地片段，保留会话切换、真实等待、语音回答和 Ground truth 对比。</p>
<video id="movie" controls playsinline preload="metadata" src="{entry['file']}" poster="{entry['poster']}"></video>
<div class="chapters">{buttons}</div>
<nav><a download href="{entry['file']}">下载完整 MP4</a><a href="manifest.json">完整链路索引</a><a href="benchmark-evidence-check.json">输入与回答核验</a><a href="verification.json">播放检查</a></nav>
<p class="note">原速、单次连续录制；导出只裁去登录等前后准备，两个题目之间未剪切。总计 {entry['framesSent']} 帧实际输入。每题均重新观看 DAY1 11:09:42–11:14:30，约 4 分 48 秒；视频原声关闭。提问通过文字输入，演示者朗读为合成音，助手为本轮真实输出，声音按日志近似对齐。</p>
<p class="note">题目沿用已展示的 #81 和 #102，结果按本次新运行原样保留。Q81 未提供姓名身份表；Q102 的另一个目标时刻 11:16:52 不在本地视频内。允许无法确定，未输入全天历史，不作为官方完整 benchmark 成绩。标准答案在回答保存后才揭晓。</p>
<details><summary>展开本轮实际回答与 Ground truth</summary><table><tr><th>官方原题</th><th>系统实际回答</th><th>Ground truth</th><th>对比</th></tr>{rows}</table></details>
<p class="note"><a href="../{source['folder'].name}/report.json">原始运行报告</a> · <a href="../{source['folder'].name}/frame-feed.jsonl">逐帧输入记录</a></p></main>
<script>function seekTo(t){{const v=document.getElementById('movie');v.currentTime=t;v.play();}}</script></html>'''
    (out/'index.html').write_text(page,encoding='utf-8')
    print(json.dumps({'file':str(out/entry['file']),'durationSeconds':entry['durationSeconds'],'framesSent':entry['framesSent'],'chapters':chapters},ensure_ascii=True),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,default=ROOT/'outputs/egolife-long-video-20260916')
    main(p.parse_args())
