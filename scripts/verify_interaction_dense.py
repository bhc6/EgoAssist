"""Check the full 16-frame interaction budget on actual EgoLife video samples."""
import argparse
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import av
from PIL import Image, ImageDraw
from egoassist._env import load_env
from egoassist.core.journal import SessionJournal
from egoassist.realtime.scene import SharedScene


async def verify(source, output):
    output.mkdir(parents=True, exist_ok=False)
    scene = SharedScene(SessionJournal(output/'scene.jsonl'), os.getenv('QWEN_API_KEY') or os.getenv('DASHSCOPE_API_KEY'))
    report = {'passed': False, 'source': str(source.resolve()), 'realVideo': True, 'realModel': True,
              'scope': 'frame selection and visual inference only; not a benchmark score'}
    try:
        next_at = 0
        with av.open(str(source)) as container:
            for frame in container.decode(video=0):
                if frame.time is None or frame.time < next_at:continue
                if frame.time > 12.1:break
                image = frame.to_image();image.thumbnail((768,768));data=io.BytesIO();image.save(data,format='JPEG')
                scene.append_frame(base64.b64encode(data.getvalue()).decode(),
                    {'clockId':'egolife-replay','monotonicMs':frame.time*1000},describe=False)
                next_at += .25
        row=scene.capture('sequence',explicit=True,metadata={'clockId':'egolife-replay','monotonicMs':12050})
        report['window']=row['interactionWindow']
        assert len(report['window']['frames']) == 16
        report['context']=await scene.context('sequence',describe_frame=True,question='刚才这段画面里发生了什么变化？按先后说出可见的动作，不要猜测没看到的事情。')
        assert report['context']['visual']['status']=='observed', report['context']['visual']
        sheet=Image.new('RGB',(960,720),'white');draw=ImageDraw.Draw(sheet)
        for i,ref in enumerate(report['window']['frames']):
            image=Image.open(io.BytesIO(scene.media(ref)));image.thumbnail((240,150));x=i%4*240;y=i//4*180
            sheet.paste(image,(x,y));draw.text((x,y+152),str(ref['offsetMs'])+' ms',fill='black')
        sheet.save(output/'contact.jpg')
        report['passed']=True
    finally:
        await scene.close()
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'passed':report['passed'],'model':report['context']['visual']['model'],'latencyMs':report['context']['visual']['latencyMs']},ensure_ascii=True))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();load_env();asyncio.run(verify(args.source,args.output))
