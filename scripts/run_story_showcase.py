"""Exercise the real console and providers; retain evidence for four demo stories.

Scenario statements are staged inputs, not field studies or verified physical acts.
No model, endpoint, memory, or task state is mocked or inserted directly.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import sys
import time
import traceback
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright
from egoassist._env import load_env
from verify_real_voice_stop import synthesize
from verify_shared_scene import decode


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def render(report, output):
    esc = lambda value: html.escape(str(value))
    sections = []
    for case in report["cases"]:
        turns = []
        for step in case["steps"]:
            audio = "".join(f'<audio controls preload="none" src="{esc(p)}"></audio>' for p in step.get("audio", []))
            turns.append(f'<article><h3>{esc(step["label"])}</h3><p class="input">输入：{esc(step.get("input", ""))}</p>'
                         f'<p class="answer">{esc(step.get("answer", ""))}</p>{audio}'
                         f'<p class="meta">{esc(step.get("elapsedMs", ""))} ms · turnId: {esc(step.get("turnId", ""))}</p>'
                         f'<p class="error">{esc(step.get("error", ""))}</p></article>')
        media = "".join(f'<a href="{esc(p)}"><img loading="lazy" src="{esc(p)}" alt="{esc(case["title"])} 实际控制台截图"></a>' for p in case.get("screenshots", []))
        sections.append(f'<section id="{esc(case["id"])}"><h2>{esc(case["title"])} <small>{"通过" if case.get("passed") else "未通过"}</small></h2>'
                        f'<p>{esc(case["scope"])}</p><p class="meta">案例 {esc(case.get("sessionId", ""))}</p>'
                        + "".join(turns) + media + f'<p class="error">{esc(case.get("error", ""))}</p></section>')
    checks = "".join(f'<li>{esc(c["name"])}：{"通过" if c["passed"] else "未通过"}</li>' for c in report["checks"])
    videos = "".join(f'<video controls preload="none" src="{esc(p)}"></video>' for p in report.get("videos", []))
    content = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EgoAssist 四场景真实链路展示</title><style>
*{box-sizing:border-box}body{margin:0;color:#202321;background:#f7f8fa;font:16px/1.75 system-ui,"Microsoft YaHei",sans-serif;letter-spacing:0}
header{background:#fff;border-bottom:1px solid #dadddf;padding:28px max(20px,calc((100% - 1100px)/2))}main{max-width:1100px;margin:auto;padding:0 20px 40px}
h1{font-size:28px;margin:0}h2{font-size:23px}h3{font-size:17px;margin:0}a{color:#1559a6}nav{display:flex;gap:20px;flex-wrap:wrap;margin-top:16px}
section{padding:24px 0;border-bottom:1px solid #ccd1d5}article{padding:16px 0;border-top:1px solid #e1e4e7}p{overflow-wrap:anywhere;white-space:pre-wrap}
.meta,small{font-size:13px;color:#59626d}.answer{border-left:3px solid #258566;padding-left:16px}.input{color:#454b56}.error{color:#a52c2c}
img,video{display:block;max-width:100%;max-height:700px;object-fit:contain;margin:20px auto}audio{width:100%;max-width:520px;display:block;margin:8px 0}
@media(max-width:600px){h1{font-size:24px}header{padding:20px}nav{gap:12px}}
</style><header><h1>EgoAssist · 四场景真实链路展示</h1>'''
    content += f'<p>运行时间：{esc(report["startedAt"])} · 真实云端模型 · 真实浏览器 · {esc(report["baseUrl"])}</p>'
    content += '<p>情境输入用于产品演示，不是老人、残障人士或职业用户的实地试验。视频来自已下载的 EgoLife 数据；测试语音由 TTS 生成。任务完成表示用户确认，不能证明实物动作。</p>'
    content += f'<nav><a href="{esc(report["baseUrl"])}" target="_blank">现场控制台</a><a href="#elder">老人</a><a href="#career">职业生涯</a><a href="#life">生活助手</a><a href="#access">无障碍</a><a href="report.json">完整结果 JSON</a></nav></header><main>'
    content += "".join(sections) + f'<section><h2>核验记录</h2><ul>{checks}</ul><p>设备：{esc(json.dumps(report.get("devices", {}), ensure_ascii=False))}</p></section>'
    content += f'<section><h2>原始浏览器录屏</h2>{videos}<p>录屏不含音轨；每轮实际模型输出音频位于对应回答下方。</p></section></main></html>'
    (output / "index.html").write_text(content, encoding="utf-8")


class Showcase:
    def __init__(self, args):
        self.args = args
        self.output = args.output.resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.report = {"startedAt": datetime.now(timezone.utc).isoformat(), "baseUrl": args.base_url,
                       "mock": False, "fieldStudy": False, "passed": False, "cases": [], "checks": [], "pageErrors": []}
        self.audio = {}
        self.current = None
        self.events_file = (self.output / "browser-events.jsonl").open("w", encoding="utf-8")

    def checkpoint(self):
        save(self.output / "report.json", self.report)
        render(self.report, self.output)

    def received(self, payload):
        try:
            event = json.loads(payload)
            if event.get("type") == "audio" and event.get("audio"):
                self.audio.setdefault(event.get("responseId", "unknown"), bytearray()).extend(base64.b64decode(event["audio"]))
                event["audioBytes"] = len(base64.b64decode(event.pop("audio")))
            if event.get("type") == "state":
                return
            event["capturedAt"] = datetime.now(timezone.utc).isoformat()
            self.events_file.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.events_file.flush()
        except (ValueError, TypeError):
            pass

    def check(self, name, value, detail=None):
        self.report["checks"].append({"name": name, "passed": bool(value), "detail": detail})
        self.checkpoint()
        if not value:
            raise AssertionError(name + ": " + str(detail)[:700])

    async def screenshot(self, name, view="conversation"):
        await self.page.locator(f'a[href="#/{view}"]').click()
        path = name + ".png"
        await self.page.screenshot(path=str(self.output / path), full_page=True)
        self.current.setdefault("screenshots", []).append(path)

    async def new_case(self, id, title, scope):
        self.current = {"id": id, "title": title, "scope": scope, "steps": [], "passed": False}
        self.report["cases"].append(self.current)
        await self.page.locator('a[href="#/conversation"]').click()
        self.page.once("dialog", lambda dialog: dialog.accept(title))
        await self.page.locator("#newSession").click()
        await self.page.wait_for_function("title => document.querySelector('#sessionSelect').selectedOptions[0]?.textContent.startsWith(title + ' · ')", arg=title)
        await self.page.wait_for_function("state?.workLedger && !sessionSwitchBusy")
        self.current["sessionId"] = await self.page.evaluate("selectedSessionId")
        self.checkpoint()

    async def idle(self):
        await self.page.wait_for_function("state?.workLedger && !state.runtimeLanes?.interaction?.scheduler?.pending && !state.runtimeLanes?.interaction?.scheduler?.activeResponseIds?.length && playbackSources.size === 0", timeout=60000)

    async def ask(self, label, text, expected=(), audio_input=None):
        await self.idle()
        step = {"label": label, "input": text, "inputMode": "synthetic-speech" if audio_input else "typed", "passed": False}
        self.current["steps"].append(step)
        self.checkpoint()
        started = time.perf_counter()
        try:
            old_ids = await self.page.evaluate("state.workLedger.turnInbox.map(t=>t.turnId)")
            if audio_input:
                await self.page.evaluate("async()=>{await connectRealtime({media:false});}")
                pcm, metadata = await synthesize(text, self.output, audio_input)
                step["speechSource"] = metadata
                for offset in range(0, len(pcm) + 32000, 3200):
                    packet = (pcm + bytes(32000))[offset:offset + 3200]
                    await self.page.evaluate("audio=>socket.send(JSON.stringify({type:'audio',audio}))", base64.b64encode(packet).decode())
                    await asyncio.sleep(0.1)
            else:
                await self.page.locator("#textTurnInput").fill(text)
                await self.page.locator("#textTurnSend").click()
            await self.page.wait_for_function("ids=>state.workLedger.turnInbox.some(t=>!ids.includes(t.turnId))", arg=old_ids, timeout=60000)
            turn_id = await self.page.evaluate("ids=>state.workLedger.turnInbox.find(t=>!ids.includes(t.turnId)).turnId", old_ids)
            step["turnId"] = turn_id
            await self.page.wait_for_function("id=>{const t=state.workLedger.turnInbox.find(t=>t.turnId===id);return t?.answer?.status==='completed' || ['response.done','completed','failed','cancelled','silent'].includes(t?.delivery?.status)}", arg=turn_id, timeout=90000)
            await self.idle()
            state = await (await self.page.request.get(self.args.base_url + "/api/state?session=" + self.current["sessionId"])).json()
            turn = next(t for t in state["workLedger"]["turnInbox"] if t["turnId"] == turn_id)
            step["turn"] = turn
            step["answer"] = (turn.get("answer") or {}).get("text", "")
            rows = self.journal(self.current["sessionId"])
            if not step["answer"] and (turn.get("delivery") or {}).get("channel") == "memory-receipt":
                response_id = turn["delivery"].get("responseId")
                step["answer"] = next((r["message"]["content"] for r in reversed(rows)
                    if r.get("type") == "message" and r.get("responseId") == response_id), "")
            if not step["answer"]:
                step["answer"] = "本轮没有生成文字回答；执行结果见任务状态与日志。"
            step["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
            step["task"] = state.get("task")
            step["memory"] = state.get("episodicMemory", {}).get("timelineViews", {})
            save(self.output / f'{self.current["id"]}-{len(self.current["steps"]):02d}-state.json', state)
            rows = self.journal(self.current["sessionId"])
            responses = {r.get("responseId") for r in rows if r.get("eventType") == "response.finished" and r.get("payload", {}).get("origin", {}).get("turnId") == turn_id}
            step["responseIds"] = sorted(r for r in responses if r)
            step["audio"] = []
            for response_id in step["responseIds"]:
                if self.audio.get(response_id):
                    name = f'{self.current["id"]}-{len(self.current["steps"]):02d}-{response_id[-8:]}.wav'
                    with wave.open(str(self.output / name), "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(24000)
                        wav.writeframes(bytes(self.audio[response_id]))
                    step["audio"].append(name)
            self.check(label + "：回答包含必要事实", all(term in step["answer"] for term in expected), step["answer"])
            self.check(label + "：存在持久输入证据", any(r.get("turnId") == turn_id or r.get("payload", {}).get("turnId") == turn_id for r in rows))
            step["passed"] = True
            print(json.dumps({"case": self.current["id"], "step": label, "answer": step["answer"], "elapsedMs": step["elapsedMs"]}, ensure_ascii=True), flush=True)
            return step
        except Exception as exc:
            step["error"] = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            self.checkpoint()

    def journal(self, sid):
        path = next(self.args.data_root.glob(f"users/*/sessions/{sid}.jsonl"))
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    async def elder(self):
        await self.new_case("elder", "01 老人 · 物品回忆与纠正", "演示情境：保存备用钥匙位置，更正后刷新再问；验证个人记忆，不代表已进行老人实地试验。")
        await self.ask("保存位置", "请记入长期记忆：我的备用钥匙放在玄关绿色收纳盒里。")
        await self.ask("第一次回忆", "我的备用钥匙放在哪里？", ("绿色",))
        await self.ask("纠正位置", "更正刚才那条备用钥匙位置记忆：我的备用钥匙现在放在书房黄色抽屉里，不在玄关绿色收纳盒里。请更新长期记忆。")
        await self.page.reload()
        await self.page.wait_for_function("state?.workLedger")
        step = await self.ask("刷新后回忆", "我的备用钥匙现在放在哪里？", ("书房", "黄色"))
        cards = step["memory"].get("participant", {}).get("recent", [])
        self.check("老人：新记忆有来源", any(c.get("sourceIds") and "黄色" in c.get("text", "") for c in cards))
        self.check("老人：旧位置已被版本化替换", any(c.get("supersedes") and "黄色" in c.get("text", "") for c in cards)
                   and not any(c.get("status") == "active" and c.get("text") == "我的备用钥匙放在玄关绿色收纳盒里。" for c in cards))
        await self.screenshot("elder-memory")

    async def career(self):
        await self.new_case("career", "02 职业生涯 · 工作记录与经验回顾", "真实本次工作记录：视觉记忆闭环验收已经通过，老人和残障人士用户研究尚未进行。报告必须区分完成与待办。")
        await self.ask("记录真实工作", "请保存今天的工作记录：EgoAssist 的真实视频与记忆冲突澄清测试已通过，现场比对耗时约2.35秒。老人和残障人士的真实用户研究尚未进行。")
        await self.ask("工作状态查询", "刚才记录的老人和残障人士真实用户研究完成了吗？请按已有记录回答。", ("未",))
        await self.page.locator('a[href="#/reports"]').click()
        async with self.page.expect_response(lambda r: r.url.endswith("/api/insights") and r.request.method == "POST") as pending:
            await self.page.locator("#insightGenerate").click()
        response = await pending.value
        accepted = await response.json()
        self.check("职业：报告已受理", response.status == 202, accepted)
        job_id = accepted["job"]["jobId"]
        await self.page.wait_for_function("id=>{const r=document.querySelector('[data-insight-job=\"'+id+'\"]');return r && /已完成|失败|无记录/.test(r.textContent)}", arg=job_id, timeout=90000)
        result = await (await self.page.request.get(self.args.base_url + "/api/insights?session=" + self.current["sessionId"])).json()
        self.current["report"] = result
        final = result["results"][-1]
        self.check("职业：报告真实模型完成且有引用", final.get("status") == "completed" and bool(final.get("sources")), final)
        summary = json.dumps(final.get("summary"), ensure_ascii=False)
        self.check("职业：报告保留未完成状态", "未" in summary and "研究" in summary, summary)
        await self.screenshot("career-report", "reports")

    async def life(self):
        await self.new_case("life", "03 生活助手 · 出门核对与恢复", "演示情境：通勤清单、逐项找到、确认携带；动作来自测试者的明确口头确认，不代表摄像头证实已装包。")
        await self.ask("开始通勤核对", "我要开始通勤出门核对，需要带钥匙、手机、门禁卡。请带我一步一步检查。")
        await self.ask("确认本次清单", "我确认本次清单就是钥匙、手机、门禁卡，确认清单这一步完成了。")
        await self.ask("确认已找到", "我确认钥匙、手机、门禁卡都已经找到，逐项找到这一步完成了。")
        await self.ask("暂停任务", "暂停当前任务。")
        await self.page.reload()
        await self.page.wait_for_function("state?.task?.paused")
        self.check("生活：刷新后仍保留暂停状态", await self.page.evaluate("state.task.paused"))
        await self.ask("恢复任务", "继续当前任务。")
        step = await self.ask("确认携带", "我确认清单上的钥匙、手机、门禁卡已经全部放进随身包里，确认携带这一步完成了。")
        self.current["finalTask"] = step["task"]
        self.check("生活：三步任务经确认完成", step["task"]["completed"])
        await self.screenshot("life-task", "tasks")

    async def access(self):
        await self.new_case("access", "04 无障碍 · 声画转述与语音输入", "EgoLife 真实视频帧送入视觉链路，模型给出文字和语音；TTS 测试语音经过真实 ASR。验证交互通道，不等于残障用户效果验证。")
        source = ROOT / "data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4"
        _, picture = decode(source)
        (self.output / "egolife-input.jpg").write_bytes(picture)
        self.current["visualSource"] = {"path": str(source), "frameSeconds": 3, "sha256": hashlib.sha256(picture).hexdigest()}
        await self.page.locator("#ambientGrant").click()
        await self.page.evaluate("async()=>{await connectRealtime({media:false});}")
        await self.page.evaluate("image=>socket.send(JSON.stringify({type:'video',image}))", base64.b64encode(picture).decode())
        await self.ask("画面转为可读可听描述", "请描述刚刚这张画面里实际可见的桌面和主要物品。不要猜测人物姓名。", ("桌",))
        speech = await self.ask("语音输入真实转写", "请只回答七乘八的结果。", ("56",), audio_input="access-question")
        self.check("无障碍：输入确实经过语音转写", speech["turn"]["source"] == "input-audio-transcript")
        self.check("无障碍：回答有真实音频", bool(speech["audio"]))
        await self.screenshot("access-conversation")
        await self.page.set_viewport_size({"width": 390, "height": 844})
        await self.screenshot("access-mobile")
        self.check("移动页面无横向溢出", await self.page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
        await self.page.set_viewport_size({"width": 1440, "height": 1000})

    async def devices(self):
        await self.new_case("devices", "05 电脑现场设备检查", "真实电脑摄像头和麦克风：短时检查硬件流、画面和信号；不将环境声音当作已验证的人声指令。")
        await self.page.locator("#cameraButton").click()

        await self.page.wait_for_function("document.querySelector('#video').srcObject?.getVideoTracks().some(t=>t.readyState==='live')", timeout=20000)
        await self.page.wait_for_function("document.querySelector('#video').videoWidth > 0", timeout=20000)
        self.report["devices"] = await self.page.evaluate("""async()=>{
            const video=document.querySelector('#video'), stream=video.srcObject;
            const tracks=stream.getTracks().map(t=>({kind:t.kind,label:t.label,readyState:t.readyState,settings:t.getSettings()}));
            const canvas=document.createElement('canvas');canvas.width=160;canvas.height=90;
            const ctx=canvas.getContext('2d');ctx.drawImage(video,0,0,160,90);
            const data=ctx.getImageData(0,0,160,90).data;
            let sum=0,sum2=0,n=0;for(let i=0;i<data.length;i+=4){const x=(data[i]+data[i+1]+data[i+2])/3;sum+=x;sum2+=x*x;n++}
            return {tracks,width:video.videoWidth,height:video.videoHeight,pixelVariance:sum2/n-(sum/n)**2,physicalSpeechRecognized:false};
        }""")
        self.check("电脑：真实摄像头输出非空白", self.report["devices"]["pixelVariance"] > 1, self.report["devices"])
        self.check("电脑：存在真实麦克风轨道", any(t["kind"] == "audio" and t["readyState"] == "live" for t in self.report["devices"]["tracks"]))
        await self.screenshot("physical-camera")
        await self.page.locator("#cameraButton").click()

    async def handoff(self):
        await self.new_case("handoff", "06 现场 · 重启后回忆与实时摄像头", "沿用已核验账户，新建会话回忆已有长期记忆，再读取真实电脑摄像头画面。")
        await self.ask("服务重启后的跨会话回忆", "我的备用钥匙现在放在哪里？", ("书房", "黄色"))
        await self.page.locator("#microphoneEnabled").uncheck()
        await self.page.locator("#cameraButton").click()
        try:
            await self.page.wait_for_function("document.querySelector('#video').videoWidth > 0", timeout=20000)
            await self.page.wait_for_function("state?.sharedScene?.camera === 'receiving'", timeout=20000)
            self.check("现场：关闭麦克风保留摄像头", await self.page.evaluate("stream.getAudioTracks().every(t=>!t.enabled) && stream.getVideoTracks().every(t=>t.enabled)"))
            await self.ask("电脑摄像头的实际现场回答", "请用两句话描述摄像头当前看到的室内环境，不要猜测人物身份。")
            await self.screenshot("live-camera-answer")
        finally:
            await self.page.locator("#cameraButton").click()
            await self.page.locator("#microphoneEnabled").check()

    async def run(self):
        load_env(str(ROOT / ".env"))
        self.checkpoint()
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True, args=["--use-fake-ui-for-media-stream"])
            context = await browser.new_context(viewport={"width": 1440, "height": 1000}, record_video_dir=str(self.output / "recordings"), record_video_size={"width": 1440, "height": 1000}, permissions=["camera", "microphone"])
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)
            self.page = await context.new_page()
            self.page.on("pageerror", lambda error: self.report["pageErrors"].append(str(error)))
            self.page.on("websocket", lambda ws: ws.on("framereceived", self.received))
            try:
                await self.page.goto(self.args.base_url)
                await self.page.locator("#loginMode" if self.args.login else "#registerMode").click()
                await self.page.locator("#authUsername").fill(self.args.username)
                await self.page.locator("#authPassword").fill(self.args.password)
                await self.page.locator("#authSubmit").click()
                await self.page.locator("#authGate").wait_for(state="hidden")
                await self.page.wait_for_function("state?.workLedger")
                actions = {name: getattr(self, name) for name in ("elder", "career", "life", "access", "devices", "handoff")}
                for action in (actions[name] for name in self.args.cases.split(",")):
                    try:
                        await action()
                        self.current["passed"] = True
                    except Exception as exc:
                        self.current["error"] = type(exc).__name__ + ": " + str(exc)
                        self.current["traceback"] = traceback.format_exc()
                        print(json.dumps({"case": self.current["id"], "error": self.current["error"]}, ensure_ascii=True), flush=True)
                        await self.page.screenshot(path=str(self.output / (self.current["id"] + "-failure.png")), full_page=True)
                        await self.page.reload()
                        await self.page.wait_for_function("state?.workLedger")
                    self.checkpoint()
                await context.storage_state(path=str(self.output / "browser-auth.json"))
            finally:
                await context.tracing.stop(path=str(self.output / "playwright-trace.zip"))
                await context.close()
                await browser.close()
                self.events_file.close()
        self.report["videos"] = [str(p.relative_to(self.output)).replace("\\", "/") for p in (self.output / "recordings").glob("*.webm")]
        logs = self.output / "journals"
        logs.mkdir(exist_ok=True)
        for case in self.report["cases"]:
            if case.get("sessionId"):
                save(logs / (case["id"] + ".json"), self.journal(case["sessionId"]))
        self.report["passed"] = all(c.get("passed") for c in self.report["cases"]) and not self.report["pageErrors"]
        self.report["finishedAt"] = datetime.now(timezone.utc).isoformat()
        self.checkpoint()
        print(json.dumps({"passed": self.report["passed"], "output": str(self.output)}), flush=True)
        return self.report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8770")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--username", default="story-demo")
    parser.add_argument("--password", default="EgoAssist-demo-2026")
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--cases", default="elder,career,life,access,devices")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(Showcase(args).run()) else 1)
