"""Record everyday, multi-turn stories through the real browser and providers.

The demonstrator is voiced by TTS; ordinary turns enter through the text UI.
The accessibility voice turn instead traverses real streaming ASR. No assistant
answers are scripted. Memory receipts are read verbatim by explicitly marked TTS.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import wave

from run_story_showcase import ROOT, Showcase, load_env
from verify_real_voice_stop import synthesize
from verify_shared_scene import decode


STORIES = {
    'elder': {
        'title': '老人 · 陪我记住家里的小事',
        'scope': '十轮生活化对话：区分日常钥匙与备用钥匙，改口纠正位置，刷新后继续回忆。演示者为合成语音，尚未开展老人用户研究。',
        'turns': [
            '刚收拾完屋子，有几样东西换了地方，我怕过两天又忘了。你陪我聊聊吧，咱们慢慢说，一次一两句就行。',
            '备用钥匙我放到玄关那个绿色的小盒子里了，你帮我记住这个位置。',
            '我平常出门带的那串还在包里，这把是留在家里的备用钥匙，别弄混了。',
            '那你说说，备用的那把，我下回应该去哪里找？',
            '哎，我刚又想了想，放门口还是不合适。现在已经挪到书房的黄色抽屉里了，原来的位置不算了，你帮我改过来。',
            '我这记性，刚说完就有点糊涂了。现在是去门口那个盒子，还是去书房？',
            '那个绿色盒子我还留着装小东西，别把盒子也记成搬走了。',
            '我回来了，刚才页面不小心关了。你还记得我家的备用钥匙放哪儿吗？',
            '我准备跟老伴说一声，怕他说听不明白，你替我说得简单一点。',
            '这样我就知道了。对了，我平常出门带的那串呢？我前面跟你说过的，你还记得吗？',
        ],
    },
    'career': {
        'title': '职业成长 · 把忙碌变成说得清的经历',
        'scope': '十轮工作复盘：基于已完成的五段真实视频工作，逐步整理周报与项目经历；未进行的用户研究保持未完成。',
        'turns': [
            '今天忙了一整天，感觉做了不少事，可一想到写周报就脑子空白。你陪我捋一捋，别一下给我一大篇。',
            '今天做的是 EgoAssist 的演示，我已经把演示拆成五段视频了，都是系统真实跑出来的。你先帮我记下来。',
            '最花时间的是把每一轮回答的声音和录屏对上，还把五段都在浏览器里检查能不能播。这个也帮我记一下。',
            '但老人和残障人士的真实体验还没做。这是接下来要补的，不能写成已经验证有效。',
            '那你帮我挑两件已经做完的事，我跟同事说起来别太散。',
            '要是写成已经证明能帮助老人生活，是不是有点说过头了？',
            '嗯，就按实际做的来。你帮我写一小段周报，我想直接读给同事听，口语一点。',
            '再短一点吧，像我自己说话那样。做完的和还没做的都留着。',
            '这次经历以后能放进我的项目介绍里吗？给我一句朴素一点的说法，不要往里面加没做过的事。',
            '好，下一步我应该先验证什么？先说最关键的一件就行。',
        ],
    },
    'life': {
        'title': '生活助手 · 出门前的几分钟',
        'scope': '十轮出门检查：找到不等于带好，中途被打断，刷新后接着检查。物品状态来自演示者口头陈述。',
        'turns': [
            '我快迟到了，出门前脑子有点乱。帮我做个出门检查吧，今天要带钥匙、手机和门禁卡，你一个一个提醒我。',
            '对，今天就这三样，别再加东西了。',
            '钥匙和手机都找到了，门禁卡还没看到。我先翻一下昨天穿的外套。',
            '找到了，在外套口袋里，三样现在都摆在桌上了。',
            '等一下，有人敲门，我先去看一眼，回来再接着弄。',
            '好了，我回来了。接着刚才的来吧，我还差什么没弄？',
            '我把钥匙放进包里了，手机拿在手上，这样算都带好了吗？',
            '哦对，门禁卡还在桌上。你刚才要是不提醒，我真就走了。',
            '现在门禁卡也装进包里了，钥匙在包里，手机在我手上，三样都带着了。',
            '我再听你说一遍，今天这三样分别在哪儿？说完我就出门了。',
        ],
    },
    'access': {
        'title': '无障碍 · 你慢慢说，我跟着听',
        'scope': '八轮图片辅助阅读：从概览到细节，追问颜色、物品、文字与身份边界；其中一轮合成测试语音经过真实 ASR。',
        'turns': [
            '我看这张桌子的照片有点费劲，你先告诉我大概是什么场景，慢一点，别一下说太多。',
            '桌子上最显眼的是什么？挑两三样跟我说就好。',
            '你说的那块桌布，是什么颜色的？',
            '我想找笔，照片里能看清有没有吗？看不清你就直说，别让我白找。',
            '那上面的字能读清楚吗？要是太小，就告诉我看不清。',
            '能看出来他们都叫什么名字吗？',
            '那你就用一句话，再告诉我桌子上有什么。',
            '听清楚了。以后遇到这种照片，先说大概，再问我要不要听细节，这样我比较好跟上。',
        ],
    },
}


def utc():
    return datetime.now(timezone.utc).isoformat()


class NaturalShowcase(Showcase):
    async def prepare(self):
        load_env(str(ROOT / '.env'))
        self.fixtures = {}
        semaphore = asyncio.Semaphore(3)

        async def prepare_one(case_id, index, text):
            async with semaphore:
                name = f'{case_id}-{index:02d}'
                if self.args.fixture_dir:
                    old = json.loads((self.args.fixture_dir / 'report.json').read_text(encoding='utf-8'))
                    old_case = next((c for c in old['cases'] if c['id'] == case_id), {})
                    match = next((s for s in old_case.get('steps', []) if s['input'] == text and s.get('demonstratorAudio')), None)
                    if match:
                        from verify_real_voice_stop import decode_fixture
                        source = self.args.fixture_dir / match['demonstratorAudio']
                        shutil.copyfile(source, self.output / f'input-{name}.wav')
                        pcm, meta = decode_fixture(source.read_bytes())
                        self.fixtures[name] = (pcm, {**meta, 'reusedFrom': str(source)})
                        return
                pcm, meta = await synthesize(text, self.output, name)
                self.fixtures[name] = (pcm, meta)
                print(f'Prepared demonstrator voice: {name}', flush=True)

        await asyncio.gather(*(prepare_one(case, i, text)
                              for case in self.args.cases.split(',')
                              for i, text in enumerate(STORIES[case]['turns'], 1)))

    async def spoken_turn(self, case_id, index, text, voice_input=False):
        name = f'{case_id}-{index:02d}'
        pcm, metadata = self.fixtures[name]
        await self.idle()
        read_start = utc()
        if voice_input:
            # Reuse the existing audited implementation, with the prepared fixture.
            # Capture the actual start of audio transmission in the event evidence.
            import run_story_showcase
            original = run_story_showcase.synthesize

            async def prepared(*_args, **_kwargs):
                return pcm, metadata

            run_story_showcase.synthesize = prepared
            try:
                step = await super().ask(f'第 {index} 轮', text, audio_input=name)
            finally:
                run_story_showcase.synthesize = original
        else:
            await self.page.locator('#textTurnInput').fill(text)
            # The synthetic user voice is a presentation readout, not physical mic evidence.
            await asyncio.sleep(len(pcm) / 32000)
            step = await super().ask(f'第 {index} 轮', text)
        step['demonstratorAudio'] = f'input-{name}.wav'
        step['demonstratorAudioStartedAt'] = read_start
        step['demonstratorSpeechSource'] = metadata
        if not step['audio'] and step['answer'] and (step['turn'].get('delivery') or {}).get('channel') == 'memory-receipt':
            receipt_name = f'receipt-{name}'
            cached = None
            if self.args.fixture_dir:
                old = json.loads((self.args.fixture_dir / 'report.json').read_text(encoding='utf-8'))
                cached = next((s for c in old['cases'] for s in c['steps']
                               if s.get('answer') == step['answer'] and s.get('receiptReadout')), None)
            if cached:
                from verify_real_voice_stop import decode_fixture
                original_path = self.args.fixture_dir / cached['receiptReadout']['file']
                shutil.copyfile(original_path, self.output / f'input-{receipt_name}.wav')
                receipt_pcm, receipt_meta = decode_fixture(original_path.read_bytes())
                receipt_meta['reusedFrom'] = str(original_path)
            else:
                for attempt in range(2):
                    try:
                        receipt_pcm, receipt_meta = await synthesize(step['answer'], self.output, receipt_name, voice='Ethan')
                        break
                    except (TimeoutError, ConnectionError) as error:
                        step.setdefault('receiptTtsRetries', []).append({'error': type(error).__name__, 'at': utc()})
                        self.checkpoint()
                        if attempt:
                            raise
            step['receiptReadout'] = {'file': f'input-{receipt_name}.wav', 'startedAt': utc(),
                                      'source': receipt_meta, 'kind': 'verbatim-server-receipt-tts'}
            await asyncio.sleep(len(receipt_pcm) / 32000)
        await asyncio.sleep(.65)
        self.checkpoint()
        return step

    async def story(self, case_id):
        story = STORIES[case_id]
        await self.new_case(case_id, story['title'], story['scope'])
        self.current['recordingStartedAt'] = utc()
        self.current['naturalDialogue'] = True
        self.current['expectedTurns'] = len(story['turns'])
        picture = None
        if case_id == 'access':
            source = ROOT / 'data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4'
            _, picture = decode(source)
            (self.output / 'egolife-input.jpg').write_bytes(picture)
            self.current['visualSource'] = {'path': str(source), 'frameSeconds': 3,
                                            'sha256': hashlib.sha256(picture).hexdigest()}
            await self.page.locator('#ambientGrant').click()
            await self.page.evaluate('async()=>{await connectRealtime({media:false});}')
        for index, text in enumerate(story['turns'], 1):
            if (case_id == 'elder' and index == 8) or (case_id == 'life' and index == 6):
                self.current.setdefault('reloads', []).append({'beforeTurn': index, 'at': utc()})
                await self.page.reload()
                await self.page.wait_for_function('state?.workLedger')
            if picture:
                await self.page.evaluate("image=>socket.send(JSON.stringify({type:'video',image}))", base64.b64encode(picture).decode())
            step = await self.spoken_turn(case_id, index, text, voice_input=case_id == 'access' and index == 7)
            # Keep natural dialogue flowing; collect substantive checks separately.
            self.current.setdefault('observations', []).append({
                'turn': index, 'answer': step['answer'], 'task': step.get('task'),
                'delivery': step['turn'].get('delivery'), 'source': step['turn'].get('source'),
            })
        await self.screenshot(f'{case_id}-final')
        self.current['recordingFinishedAt'] = utc()
        self.check(f'{case_id}：全部多轮输入具有真实终态',
                   len(self.current['steps']) == len(story['turns']) and
                   all(s['turn'].get('status') == 'completed' and s.get('passed') for s in self.current['steps']))
        steps = self.current['steps']
        self.check(f'{case_id}：每轮路由有效且有实际回答', all(
            s['turn']['semantic']['status'] == 'classified' and
            not s['answer'].startswith('本轮没有生成') for s in steps))
        if case_id == 'elder':
            self.check('老人：刷新后记得更正位置', all(t in steps[7]['answer'] for t in ['书房', '黄色']))
            self.check('老人：两串钥匙不混淆', '包' in steps[9]['answer'])
        if case_id == 'life':
            self.check('生活：找到物品尚未完成携带', bool(steps[3]['task']) and not steps[3]['task']['completed'])
            self.check('生活：自然打断暂停任务', steps[4]['task']['paused'])
            self.check('生活：刷新后恢复原任务', not steps[5]['task']['paused'])
            self.check('生活：部分携带不能完成', not steps[6]['task']['completed'] and '门禁卡' in steps[6]['answer'])
            self.check('生活：全部确认后完成', steps[8]['task']['completed'])
        if case_id == 'career':
            self.check('职业：周报保留尚未开展的用户研究',
                       any(t in steps[6]['answer'] for t in ['没', '未']) and any(t in steps[6]['answer'] for t in ['体验', '研究', '用户']))
        if case_id == 'access':
            self.check('无障碍：连续话题中真实语音输入', steps[6]['turn']['source'] == 'input-audio-transcript')

    async def elder(self):
        await self.story('elder')

    async def career(self):
        await self.story('career')

    async def life(self):
        await self.story('life')

    async def access(self):
        await self.story('access')


async def main(args):
    demo = NaturalShowcase(args)
    await demo.prepare()
    return await demo.run()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8771')
    parser.add_argument('--data-root', type=Path, default=ROOT / '.egoassist-natural-demo')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--username', default='natural-story-demo')
    parser.add_argument('--password', default='Natural-story-demo-2026')
    parser.add_argument('--login', action='store_true')
    parser.add_argument('--cases', default='elder,career,life,access')
    parser.add_argument('--fixture-dir', type=Path)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args)) else 1)
