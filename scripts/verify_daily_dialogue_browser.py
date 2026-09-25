"""Real Edge playback + JEV + ASR, with authored speech and EgoLife replay.

No physical microphone/AEC measurements; source and synthesized audio are labelled.
"""
import argparse
import asyncio
import base64
from contextlib import AsyncExitStack
import hashlib
import json
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from verify_real_voice_stop import synthesize
from verify_shared_scene import decode


async def verify(output, base_url=None):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "hardwareMeasured": False, "realProviders": True,
              "pageErrors": [], "checks": {}, "sources": {}, "events": []}
    def save():
        (output / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        fixtures = {}
        for index, (name, phrase) in enumerate([
            ("ack", "嗯嗯，对，我在听，你继续讲。"),
            ("topic", "换个问题，请简短回答九乘九是多少？"),
            ("wait", "等一下，我还没说完，我想想怎么问。"),
            ("question", "助手，七加八是多少？请直接回答结果。"),
            ("stop", "先别说了，安静一会儿。"),
        ]):
            fixtures[name], metadata = await synthesize(phrase, output, index)
            report["sources"][name] = {"kind": "synthetic-speech", "text": phrase, **metadata}
        source = ROOT / "data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4"
        fixtures["egolife"], _ = decode(source, seconds=12)
        report["sources"]["egolife"] = {"kind": "recorded-EgoLife-human-meeting", "path": str(source),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "offsetSeconds": 0, "durationSeconds": 12,
            "speakerAndAddresseeGroundTruth": False}
        async with AsyncExitStack() as stack:
            if base_url:
                base = base_url.rstrip("/")
            else:
                app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / "runtime.json"),
                                 "qwen3.5-omni-flash-realtime", "Ethan")
                server = await stack.enter_async_context(TestServer(app))
                base = str(server.make_url("")).rstrip("/")
            pw = await stack.enter_async_context(async_playwright())
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: report["pageErrors"].append(str(error)))
                await page.goto(base + "/")
                await page.locator("#registerMode").click()
                await page.locator("#authUsername").fill("daily-dialogue-" + secrets.token_hex(3))
                await page.locator("#authPassword").fill(secrets.token_urlsafe(24))
                await page.locator("#authSubmit").click()
                await page.wait_for_function("state?.sessionId")
                report["sessionId"] = await page.evaluate("selectedSessionId")
                await page.evaluate("async()=>{await connectRealtime({media:false});}")
                await page.evaluate("""()=>{
                    window.dailyEvents=[];
                    socket.addEventListener('message',ev=>{
                        const e=JSON.parse(ev.data);
                        if(e.type==='state'||e.type==='memory_trace')return;
                        if(e.audio){e.audioBytes=e.audio.length;delete e.audio;}
                        dailyEvents.push({...e,atMs:performance.now(),playingSources:playbackSources.size});
                    });
                }""")

                async def typed(text):
                    await page.locator("#textTurnInput").fill(text)
                    await page.locator("#textTurnSend").click()
                    return await page.evaluate("currentTurnId")

                async def wait_audio(tid):
                    await page.wait_for_function("tid=>dailyEvents.some(e=>e.type==='audio'&&e.origin?.turnId===tid)", arg=tid, timeout=45000)

                async def stream(name):
                    start = await page.evaluate("dailyEvents.length")
                    data = fixtures[name] + bytes(32000 * 2)
                    for offset in range(0, len(data), 3200):
                        await page.evaluate("audio=>socket.send(JSON.stringify({type:'audio',audio}))",
                            base64.b64encode(data[offset:offset+3200]).decode())
                        await asyncio.sleep(.1)
                    await page.wait_for_function("start=>dailyEvents.slice(start).some(e=>e.type==='user_transcript')", arg=start, timeout=30000)
                    await page.wait_for_function("start=>dailyEvents.slice(start).some(e=>e.type==='speech_control'||e.type==='speech_floor')", arg=start, timeout=30000)
                    events = await page.evaluate("start=>dailyEvents.slice(start)", start)
                    report.setdefault("audioInputs", []).append({"name": name, "events": events})
                    save()
                    return events

                original = await typed("请详细解释乘法原理和十个生活例子，讲三十句话。")
                await wait_audio(original)
                await page.wait_for_function("playbackSources.size>0", timeout=10000)
                report["checks"]["firstQuestionAudible"] = True
                # Exercise actual local Silero VAD with speech into a MediaStream.
                await page.evaluate("""async()=>{
                    window.testVadDestination=audioContext.createMediaStreamDestination();
                    await startLocalVad(testVadDestination.stream);
                    if(!localVad)throw Error('Silero VAD unavailable');
                    const fn=localVad.options.onSpeechStart;
                    localVad.options.onSpeechStart=()=>{
                        const before=playbackSources.size; fn();
                        window.vadEvidence={before,after:playbackSources.size};
                    };
                }""")
                await page.evaluate("""audio=>{
                    const raw=atob(audio), buffer=audioContext.createBuffer(1,raw.length/2,16000);
                    const view=new DataView(Uint8Array.from(raw,c=>c.charCodeAt(0)).buffer);
                    for(let i=0;i<raw.length/2;i++)buffer.getChannelData(0)[i]=view.getInt16(i*2,true)/32768;
                    const source=audioContext.createBufferSource();source.buffer=buffer;
                    source.connect(testVadDestination);source.start();
                }""", base64.b64encode(fixtures["ack"]).decode())
                events = await stream("ack")
                report["vadEvidence"] = await page.evaluate("window.vadEvidence")
                assert report["vadEvidence"] and report["vadEvidence"]["before"] > 0
                assert report["vadEvidence"]["after"] == report["vadEvidence"]["before"]
                assert any(e["type"] == "speech_control" and e["action"] == "keep" for e in events), events
                assert not any(e["type"] == "playback_stop" for e in events)
                assert await page.evaluate("playbackSources.size>0")
                report["checks"]["backchannelAndVadPreservePlayback"] = True

                events = await stream("egolife")
                assert not any(e["type"] == "speech_floor" for e in events), events
                assert not any(e["type"] == "playback_stop" for e in events)
                report["checks"]["egolifeReplayDoesNotInterrupt"] = True

                events = await stream("topic")
                topic = next(e["turnId"] for e in events if e["type"] == "user_transcript")
                assert any(e["type"] == "playback_stop" and e.get("trigger") == "semantic-floor-change" for e in events)
                await wait_audio(topic)
                report["checks"]["spokenTopicChangeAudible"] = True
                await page.wait_for_function("tid=>dailyEvents.some(e=>e.type==='assistant_transcript'&&e.origin?.turnId===tid&&/81|八十一/.test(e.text))", arg=topic, timeout=30000)

                await stream("wait")
                assert not await page.evaluate("state?.interaction?.quiet")
                events = await stream("question")
                question = next(e["turnId"] for e in events if e["type"] == "user_transcript")
                await wait_audio(question)
                report["checks"]["temporaryWaitThenQuestionAudible"] = True

                # Consecutive inputs before the answer completes: the ack must not
                # replace the question or create a second acknowledgement answer.
                rapid = await typed("请解释一下为什么天空是蓝色的，用三句话回答。")
                await asyncio.sleep(.05)
                ack = await typed("嗯嗯，我在听，你继续。")
                await wait_audio(rapid)
                await page.wait_for_function("tid=>dailyEvents.some(e=>e.type==='speech_control'&&e.turnId===tid&&e.action==='keep')",arg=ack,timeout=30000)
                report["checks"]["rapidQuestionThenAckAudible"] = True

                events = await stream("stop")
                assert any(e["type"] == "speech_control" and e["action"] == "stop-speaking" for e in events), events
                assert await page.evaluate("playbackSources.size") == 0
                report["checks"]["semanticStopClearsDeviceQueue"] = True
                report["events"] = await page.evaluate("dailyEvents")
                state = await (await page.request.get(base + "/api/state", params={"session": report["sessionId"]})).json()
                report["turns"] = [{"turnId": t["turnId"], "text": t["text"], "delivery": t.get("delivery"),
                                    "answerStatus": (t.get("answer") or {}).get("status")} for t in state["workLedger"]["turnInbox"]]
                for width in (1440, 390):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    await page.screenshot(path=str(output / f"daily-{width}.png"))
                    assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                report["passed"] = not report["pageErrors"]
            finally:
                try:
                    report["events"] = await page.evaluate("window.dailyEvents||[]")
                except Exception:
                    pass
                await browser.close()
    except Exception as error:
        report["error"] = type(error).__name__ + ": " + str(error)[:2200]
    save()
    print(json.dumps({k:v for k,v in report.items() if k not in {"events", "audioInputs", "sources", "turns"}},ensure_ascii=True),flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output, args.base_url)) else 1)
