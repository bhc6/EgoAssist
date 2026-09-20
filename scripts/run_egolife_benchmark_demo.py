"""Record official questions on bounded local video; reveal gold after prediction.

This is an explicitly target-assisted clip demonstration, not full-history QA.
The original gold is never put into model messages. Incorrect answers are retained.
"""
from __future__ import annotations
import argparse
import asyncio
import base64
import hashlib
import json
import re
from pathlib import Path
from aiohttp import web
from run_natural_story_showcase import NaturalShowcase, STORIES, ROOT, utc
from run_story_showcase import save
from egoassist.eval.choice import explicit_choice

OFFICIAL = ROOT / 'artifacts/egolife-independent-freeze-20260914-r4/official-qa-private.json'
WINDOWS = {'81': (180, 240, '源文件 180–240 秒；事件时刻以画面水印为准', '需要人物姓名；本次未输入人物身份表。'),
           '102': (60, 120, '源文件 60–120 秒；事件时刻以画面水印为准', '只覆盖一个目标时刻；11:16:52 的另一个标注时刻不在此片段内。')}

def public(row):
    return {k: row[k] for k in ('ID', 'question', 'question_chinese', 'query_time',
            'choice_a', 'choice_b', 'choice_c', 'choice_d',
            'choice_a_chinese', 'choice_b_chinese', 'choice_c_chinese', 'choice_d_chinese')}

def prompt(q):
    options = '\n'.join(f'{c}. {q["choice_"+c.lower()+"_chinese"]}' for c in 'ABCD')
    return ('下面是 EgoLifeQA 原题，问题中的“我”指视频佩戴者 JAKE。请根据刚才实际收到的视频回答，'
            '先写“选择：A/B/C/D”中的一个字母，再简短说明依据；证据不足时写“选择：无法确定”，不要编造人物身份。\n'
            + q['question_chinese'] + '\n' + options)

class BenchmarkDemo(NaturalShowcase):
    async def benchmark(self, qid, voice_case):
        q = self.questions[qid]
        begin, end, clock, limitation = self.windows[qid]
        full = getattr(self.args, 'full_local_video', False)
        scope = ('完整本地视频原速输入，各题独立会话。' if full else '目标时刻辅助选取本地一分钟视频。')
        await self.new_case('q'+qid, 'EgoLifeQA #'+qid,
            '官方原题、原选项；' + scope + limitation)
        self.current.update(recordingStartedAt=utc(), question=q, frameFeed=[],
                            videoWindow={'startSeconds': begin, 'endSeconds': end, 'clock': clock},
                            inputProtocol=('all local video per independent question' if full else 'target-assisted clip') + ', text question, silent source video; no full-history retrieval',
                            visualSource={'path': str(self.args.media.resolve()),
                                          'sha256': hashlib.sha256(self.args.media.read_bytes()).hexdigest()})
        # Permission persists across sessions; the consent button then disappears.
        if await self.page.locator('#ambientGrant').count():
            await self.page.locator('#ambientGrant').click()
        await self.page.evaluate('async()=>{await connectRealtime({media:false});}')
        await self.page.evaluate('''async cfg=>{
          document.querySelector('#benchmarkDemo')?.remove();
          const s=document.createElement('section');s.id='benchmarkDemo';
          s.innerHTML=`<style>#benchmarkDemo{position:fixed;inset:0;z-index:90;background:#eef3f0;color:#183c33;padding:28px 34px;font:21px/1.55 'Microsoft YaHei',sans-serif;pointer-events:none;box-sizing:border-box}#benchmarkDemo h1{font-size:30px;margin:0 0 8px}#benchmarkDemo p{margin:8px 0}.muted{color:#526d63;font-size:17px}.columns{display:grid;grid-template-columns:760px 1fr;gap:28px}.box{background:white;padding:18px;border-radius:12px;margin-top:14px}#bvideo{width:760px;height:428px;background:#142920}#banswer{white-space:pre-wrap;font-size:21px}#bgold{white-space:pre-wrap;color:#285e49}</style>
          <h1>EgoLifeQA #${cfg.q.ID} · 原题作答与 Ground truth 对照</h1>
          <p class="muted">真实系统 · 原速视频输入 2 FPS · 文字提交官方中文题目及选项 · 标准答案在作答后揭晓</p>
          <div class="columns"><div><video id="bvideo" muted crossorigin="anonymous" playsinline></video>
          <p id="bfeed">准备视频…</p><div class="box"><b id="bphase">1 / 4　观看视频证据</b><p id="bstatus">画面通过实际系统 WebSocket 输入</p></div>
          <p class="muted" id="bscope"></p><p class="muted">${cfg.scope}不代表全天记忆检索。视频原声未输入。演示者朗读为合成音，助手声音为本轮原始输出。</p></div>
          <div><div class="box"><b>Benchmark 原题</b><p id="bquestion"></p><p id="benglish" class="muted"></p><div id="boptions"></div></div>
          <div class="box"><b>系统实际回答</b><div id="banswer">等待提交问题</div></div>
          <div class="box"><b>Ground truth</b><div id="bgold">隐藏，等待回答落盘后揭晓</div></div></div></div>`;
          document.body.append(s);
          document.querySelector('#bquestion').textContent=cfg.q.question_chinese;
          document.querySelector('#benglish').textContent=cfg.q.question;
          document.querySelector('#boptions').textContent='';
          for(const c of 'ABCD'){const p=document.createElement('p');p.textContent=c+'. '+cfg.q['choice_'+c.toLowerCase()+'_chinese'];document.querySelector('#boptions').append(p);}
          document.querySelector('#bscope').textContent=cfg.clock+'；'+cfg.limitation;
          const v=document.querySelector('#bvideo');v.src=cfg.url;
          await new Promise(resolve=>v.addEventListener('loadedmetadata',resolve,{once:true}));
          if(cfg.begin>0){v.currentTime=cfg.begin;await new Promise(resolve=>v.addEventListener('seeked',resolve,{once:true}));}
          window.bCount=0;window.bDone=false;window.bAnswer='';
          v.addEventListener('ended',()=>{clearInterval(window.bTimer);window.bDone=true;},{once:true});
          const canvas=document.createElement('canvas');
          const scale=768/Math.max(v.videoWidth,v.videoHeight);
          canvas.width=Math.round(v.videoWidth*scale);canvas.height=Math.round(v.videoHeight*scale);
          window.bTimer=setInterval(()=>{
            if(v.readyState<2||v.paused||!socket||socket.readyState!==1)return;
            if(v.currentTime>=cfg.end){v.pause();clearInterval(bTimer);window.bDone=true;return;}
            canvas.getContext('2d').drawImage(v,0,0,canvas.width,canvas.height);
            const image=canvas.toDataURL('image/jpeg',.8).split(',')[1],capturedAt=new Date().toISOString();
            socket.send(JSON.stringify({type:'video',image,capture:{capturedAt,monotonicMs:performance.now(),mirrored:false,width:canvas.width,height:canvas.height}}));
            bCount++;auditBenchmarkFrame({qid:cfg.q.ID,sourceSeconds:v.currentTime,capturedAt,sequence:bCount,image});
            document.querySelector('#bfeed').textContent='原视频 '+v.currentTime.toFixed(1)+' 秒 / 已发送 '+bCount+' 帧 / 原速';
          },500);
          socket.addEventListener('message',e=>{try{const m=JSON.parse(e.data);if(m.type==='assistant_transcript'){window.bAnswer=m.done?m.text:window.bAnswer+(m.text||'');document.querySelector('#banswer').textContent=window.bAnswer;}}catch{}});
          await v.play();
        }''', {'q':q,'url':self.media_url,'begin':begin,'end':end,'clock':clock,'limitation':limitation,'scope':scope})
        deadline=asyncio.get_running_loop().time()+(end-begin)+40
        last_progress=-1
        while not await self.page.evaluate('window.bDone'):
            if asyncio.get_running_loop().time()>deadline:
                raise TimeoutError('Video playback did not finish within its real duration plus 40 seconds')
            await asyncio.sleep(5)
            elapsed=await self.page.locator('#bvideo').evaluate('(v)=>v.currentTime')
            if int(elapsed//30)>last_progress:
                last_progress=int(elapsed//30)
                self.checkpoint()
                print(json.dumps({'qid':qid,'sourceSeconds':round(elapsed,1),'frames':len(self.current['frameFeed'])}),flush=True)
        await self.page.evaluate("()=>{document.querySelector('#bphase').textContent='2 / 4　提交原题与选项';document.querySelector('#bstatus').textContent='合成朗读结束后，通过系统文字输入提交；等待真实回答';}")
        step = await self.spoken_turn(voice_case, 1, prompt(q))
        # Freeze the complete prediction before reading the official evaluation fields.
        prediction={'qid':qid,'input':step['input'],'answer':step['answer'],'turnId':step['turnId'],
                    'responseIds':step['responseIds'],'savedAt':utc()}
        path=self.output/('prediction-'+qid+'.json');save(path,prediction)
        frozen_sha=hashlib.sha256(path.read_bytes()).hexdigest()
        self.current['predictionSavedAt']=prediction['savedAt']
        await self.page.evaluate("a=>{document.querySelector('#bphase').textContent='3 / 4　回答已落盘';document.querySelector('#banswer').textContent=a;document.querySelector('#bstatus').textContent='保留原始文字、响应音频、turnId 和日志。';}",step['answer'])
        await asyncio.sleep(3)
        row=next(r for r in json.loads(OFFICIAL.read_text('utf-8')) if r['ID']==qid)
        selected=explicit_choice(step['answer'])
        score={'qid':qid,'predictedChoice':selected,'goldChoice':row['answer'],
               'correct':selected==row['answer'],'scoring':'explicit choice letter, no LLM judge; unparsed/abstain counts as non-match',
               'goldOption':row['choice_'+row['answer'].lower()+'_chinese'],
               'reason':row['reason'],'reasonChinese':row['reason_chinese'],'targetTime':row['target_time'],
               'predictionSha256':frozen_sha,'revealedAt':utc(),'officialSha256':hashlib.sha256(OFFICIAL.read_bytes()).hexdigest()}
        save(self.output/('comparison-'+qid+'.json'),score);self.current['comparison']=score
        verdict='一致' if score['correct'] else ('未选择／无法确定' if selected is None else '不一致')
        display=f"{row['answer']}. {score['goldOption']}\n对比：{verdict}\n官方依据：{row['reason_chinese']}\n标注时刻：{json.dumps(row['target_time'],ensure_ascii=False)}"
        await self.page.evaluate("s=>{document.querySelector('#bphase').textContent='4 / 4　揭晓标准答案并对比';document.querySelector('#bgold').textContent=s;document.querySelector('#bstatus').textContent='错误或证据不足原样保留；本结果不计为完整 benchmark 成绩。';}",display)
        await self.page.screenshot(path=str(self.output/('q'+qid+'-comparison.png')))
        await asyncio.sleep(12)
        self.current['recordingFinishedAt']=utc()
        self.check('Q'+qid+'：输入帧和真实回答完整',len(self.current['frameFeed'])>=(end-begin)*1.9 and bool(step['audio']) and step['turn']['status']=='completed')
        self.check('Q'+qid+'：揭晓后原预测未变',hashlib.sha256(path.read_bytes()).hexdigest()==frozen_sha)
        await self.page.evaluate("()=>document.querySelector('#benchmarkDemo').remove()")
        await self.page.reload();await self.page.wait_for_function('state?.workLedger')

    async def elder(self): await self.benchmark('81','elder')
    async def career(self): await self.benchmark('102','career')

async def main(args):
    demo=BenchmarkDemo(args)
    demo.windows=dict(WINDOWS)
    if args.full_local_video:
        import av
        with av.open(str(args.media)) as container:duration=container.duration/1e6
        demo.windows={qid:(0,duration,f'完整源文件 0–{duration:.2f} 秒；事件时刻以画面水印为准',WINDOWS[qid][3]) for qid in WINDOWS}
    demo.questions={r['ID']:public(r) for r in json.loads(OFFICIAL.read_text('utf-8')) if r['ID'] in WINDOWS}
    for name,qid in [('elder','81'),('career','102')]: STORIES[name]={'turns':[prompt(demo.questions[qid])]}
    save(demo.output/'protocol.json',{'questions':demo.questions,'windows':demo.windows,'selection':'same two previously demonstrated official QA; exposed demonstration, not independent test',
         'videoSelection':'all local video, same full range for each question' if args.full_local_video else 'target-assisted one-minute windows',
         'fullBenchmark':False,'goldUsedForGeneration':False,'sourceAudio':False,'questionInput':'typed; synthetic narration only','answerPolicy':'abstention allowed; not forced-choice benchmark protocol'})
    await demo.prepare()
    app=web.Application();app.router.add_get('/video.mp4',lambda r:web.FileResponse(args.media.resolve(),headers={'Access-Control-Allow-Origin':'*'}))
    runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    demo.media_url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/video.mp4'
    original_new_case=demo.new_case
    # expose once on each page, before actual frame transmission
    async def new_case(*a):
        if not getattr(demo,'audit_exposed',False):
            async def audit(data):
                data['sha256']=hashlib.sha256(base64.b64decode(data.pop('image'))).hexdigest()
                demo.current['frameFeed'].append(data)
                with (demo.output/'frame-feed.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(data)+'\n')
            await demo.page.expose_function('auditBenchmarkFrame',audit);demo.audit_exposed=True
        await original_new_case(*a)
    demo.new_case=new_case
    try:return await demo.run()
    finally:await runner.cleanup()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--base-url',default='http://127.0.0.1:8775');p.add_argument('--data-root',type=Path,default=ROOT/'.egoassist-egolife-demo')
    p.add_argument('--username',default='benchmark-demo-20260916-r1');p.add_argument('--password',default='Benchmark-local-demo-2026')
    p.add_argument('--media',type=Path,default=ROOT/'outputs/egolife-multimodal-media/egolife-continuous.mp4')
    p.add_argument('--fixture-dir',type=Path);p.add_argument('--login',action='store_true');p.add_argument('--cases',default='elder,career')
    p.add_argument('--full-local-video',action='store_true')
    raise SystemExit(0 if asyncio.run(main(p.parse_args())) else 1)
