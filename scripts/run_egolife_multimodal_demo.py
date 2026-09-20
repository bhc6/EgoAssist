"""Real moving EgoLife frames + synthetic speech through the actual console WS."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path

from aiohttp import web
from run_natural_story_showcase import NaturalShowcase, STORIES, ROOT, utc
from run_story_showcase import save


QUESTIONS = [
    '陪我看看这段视频吧，现在大概是什么场景？',
    '桌子上最明显的两三样东西是什么？',
    '我刚才没看清，那块桌布是什么颜色的？',
    '现在镜头前这双手在做什么，你能看清吗？',
    '现在还能看到刚才说的那张桌子吗？',
    '画面里那些小字能读清吗？',
    '我有点跟不上了，用一句话说说现在最明显的东西吧。',
    '最后简单说说，这会儿哪些细节你还拿不准？',
]
STORIES['access'] = {'title': 'EgoLife · 边看视频边语音聊天', 'turns': QUESTIONS}


class EgoLifeDemo(NaturalShowcase):
    async def access(self):
        await self.new_case('egolife', 'EgoLife · 边看视频边语音聊天',
            '真实连续 EgoLife 视频播放，按 2 FPS 采样进入系统；8 轮合成语音经真实 ASR 与多模态回答。原视频现场音未送入。')
        self.current['recordingStartedAt'] = utc()
        self.current['frameFeed'] = []
        media = self.args.media.resolve()
        self.current['visualSource'] = {'path': str(media), 'sha256': hashlib.sha256(media.read_bytes()).hexdigest(),
                                        'mode': 'continuous-video-2fps', 'originalAudioForwarded': False}
        self.current['expectedTurns'] = len(QUESTIONS)
        frames_log = (self.output / 'frame-feed.jsonl').open('w', encoding='utf-8')

        async def log_frame(data):
            picture = base64.b64decode(data.pop('image'))
            data['sha256'] = hashlib.sha256(picture).hexdigest()
            self.current['frameFeed'].append(data)
            frames_log.write(json.dumps(data, ensure_ascii=False) + '\n')
            frames_log.flush()

        await self.page.expose_function('auditEgoFrame', log_frame)
        await self.page.locator('#ambientGrant').click()
        await self.page.evaluate('async()=>{await connectRealtime({media:false});}')
        # Same media payload used by the camera UI; the source is explicitly a file.
        await self.page.evaluate('''async url => {
            const section=document.createElement('section');section.id='egolifeDemo';
            section.innerHTML='<div style="font-size:27px;font-weight:700">EgoLife · 连续第一视角视频</div>' +
              '<div style="margin:8px 0;color:#52746b">原速播放 · 2 FPS 实际送入模型 · 原视频声音关闭</div>' +
              '<video id="egolifeSource" muted playsinline crossorigin="anonymous" style="width:100%;height:490px;object-fit:contain;background:#101b18"></video>'+
              '<div id="egolifeFeed" style="padding:12px;font-size:22px">等待视频帧</div>'+
              '<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;font-size:23px;line-height:1.65"><div><b>语音提问</b><p id="egoQuestion"></p></div><div><b>系统实际回答</b><p id="egoAnswer">等待提问</p></div></div>';
            Object.assign(section.style,{position:'fixed',zIndex:80,left:'0',top:'72px',width:'100%',height:'928px',overflow:'hidden',background:'#f0f5f1',padding:'24px 32px'});
            document.body.append(section);
            const v=document.querySelector('#egolifeSource');v.src=url;
            await v.play();
            const canvas=document.createElement('canvas');canvas.width=768;canvas.height=432;
            window.egoFrames=0;window.egoStart=Date.now();
            window.egoTimer=setInterval(()=>{
                if(v.paused || v.ended || v.readyState<2 || !socket || socket.readyState!==1)return;
                canvas.getContext('2d').drawImage(v,0,0,768,432);
                const image=canvas.toDataURL('image/jpeg',.8).split(',')[1];
                const capturedAt=new Date().toISOString();
                socket.send(JSON.stringify({type:'video',image,capture:{capturedAt,monotonicMs:performance.now(),mirrored:false,width:768,height:432}}));
                window.egoFrames++;
                document.querySelector('#egolifeFeed').textContent='视频 '+v.currentTime.toFixed(1)+' 秒　｜　已发送 '+egoFrames+' 帧　｜　图像 + 语音输入 → 文字 + 语音回答';
                auditEgoFrame({sourceSeconds:v.currentTime,capturedAt,sequence:egoFrames,image});
            },500);
            socket.addEventListener('message',message=>{
                try{const e=JSON.parse(message.data);
                    if(e.type==='user_transcript')document.querySelector('#egoQuestion').textContent=e.text||'';
                }catch{}
            });
        }''', self.media_url)
        try:
            await asyncio.sleep(2)
            for index, question in enumerate(QUESTIONS, 1):
                await self.page.evaluate('q=>{document.querySelector("#egoQuestion").textContent=q;document.querySelector("#egoAnswer").textContent="正在听你说…"}', question)
                step = await self.spoken_turn('access', index, question, voice_input=True)
                step['sourceSecondsAtAnswer'] = await self.page.locator('#egolifeSource').evaluate('(v)=>v.currentTime')
                await self.page.evaluate('a=>document.querySelector("#egoAnswer").textContent=a', step['answer'])
                await self.page.screenshot(path=str(self.output / f'egolife-turn-{index:02d}.png'))
                await asyncio.sleep(1.5)
            self.check('EgoLife：8 轮均为真实语音转写且具有回答音频', all(
                s['turn']['source']=='input-audio-transcript' and s['audio']
                and s['turn']['semantic']['status']=='classified' for s in self.current['steps']))
            feed=self.current['frameFeed']
            self.check('EgoLife：连续帧覆盖超过一分钟', len(feed)>120 and feed[-1]['sourceSeconds']-feed[0]['sourceSeconds']>60)
            self.check('EgoLife：不是重复静态图片', len({f['sha256'] for f in feed})>200)
            self.current['recordingFinishedAt'] = utc()
            self.current['frameFeedSummary'] = {'count':len(feed),'distinct':len({f['sha256'] for f in feed}),
                                              'firstSeconds':feed[0]['sourceSeconds'],'lastSeconds':feed[-1]['sourceSeconds']}
        finally:
            await self.page.evaluate('()=>{clearInterval(window.egoTimer);document.querySelector("#egolifeSource").pause();}')
            frames_log.close()
            self.checkpoint()


async def main(args):
    app=web.Application()
    async def media(request):
        return web.FileResponse(args.media.resolve(),headers={'Access-Control-Allow-Origin':'*'})
    app.router.add_get('/video.mp4',media)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    try:
        demo=EgoLifeDemo(args);demo.media_url=f'http://127.0.0.1:{port}/video.mp4'
        await demo.prepare()
        return await demo.run()
    finally:
        await runner.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8771')
    parser.add_argument('--data-root',type=Path,default=ROOT/'.egoassist-natural-demo')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--username',default='egolife-multimodal-r1')
    parser.add_argument('--password',default='Egolife-demo-local-2026')
    parser.add_argument('--media',type=Path,default=ROOT/'outputs/egolife-multimodal-media/egolife-continuous.mp4')
    parser.add_argument('--login',action='store_true')
    parser.add_argument('--fixture-dir',type=Path)
    parser.set_defaults(cases='access')
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args)) else 1)
