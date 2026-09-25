"""Frozen authored probes and separately labelled EgoLife ASR routing research.

EgoLife captions are activity descriptions, NOT addressee or turn-taking labels.
The dataset replay is diagnostic; authored controls have explicit expectations.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from egoassist._env import load_env
from egoassist.realtime.decisions_router import create_turn_router
from egoassist.realtime.modular import ModularProvider, settings
from verify_shared_scene import decode

CASES = [
    ("backchannel", "嗯嗯。", "keep"),
    ("understood", "对，我在听。", "keep"),
    ("encouragement", "你继续讲，我听着呢。", "keep"),
    ("third-party", "小王，你把那边的门关一下。", "listen"),
    ("wait", "等一下，我还没说完，我想想怎么问。", "wait"),
    ("stop", "先别说了，安静一会儿。", "listen"),
    ("new-topic", "先停一下，换个问题，九乘九是多少？", "reply"),
    ("correction", "不是乘法，我问的是七加八，是多少？", "reply"),
    ("backchannel-and-question", "嗯嗯，那七乘九呢？", "reply"),
    ("quoted-stop", "如果一个人说先别说了，这算没礼貌吗？", "reply"),
    ("short-question", "为什么？", "reply"),
    ("hello-no-answer", "你好。", "reply"),
    ("unfinished-then-question", "我想问，七乘八到底是多少？", "reply"),
    ("english-backchannel", "Right, I'm listening.", "keep"),
    ("english-wait", "Wait, let me finish my question.", "wait"),
    ("english-topic", "Actually, what's nine times nine?", "reply"),
    ("egolife-split-continuation", "传过去，戳一下。哎，能看到对吗？", "listen"),
    ("direct-after-others", "助手，七加八是多少？", "reply"),
]


async def verify(output, egolife):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    router = create_turn_router(key, backend="openrouter-decisions")
    audit = []
    provider = ModularProvider(key, settings(), audit.append)
    report = {"authored": [], "egolife": [], "hardwareMeasured": False,
        "scope": "Development probes, not an unbiased benchmark. EgoLife addressee labels unavailable; report all decisions without invented accuracy.",
        "hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
            ("scripts/verify_daily_dialogue_semantics.py", "egoassist/prompts/decisions_router.json")}}
    context = {"clock_context": None, "memory_draft": None,
        "previous_answer": {"turnId": "answer", "text": "详细解释七乘八和乘法原理", "status": "running"},
        "answers": [{"turnId": "answer", "text": "详细解释七乘八和乘法原理", "status": "running"}],
        "playback": {"turnId": "answer", "playedMs": 1000, "generatedMs": 20000, "complete": False},
        "output_permission": {"quiet": False, "paused": False},
        "input_channel": "input-audio-transcript",
        "scene": {"speaker": {"status": "unknown", "wearer": False}}}
    (output / "plan.json").write_text(json.dumps({"cases": CASES, "context": context, **report}, ensure_ascii=False, indent=2), encoding="utf-8")
    def save():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        for name, text, expected in CASES:
            current = dict(context)
            if name == "hello-no-answer":
                current.update(previous_answer=None, answers=[], playback=None)
            if name in {"egolife-split-continuation", "direct-after-others"}:
                current["recent_interactions"] = [{"turnId": "prior", "text": "都戳一下，每人戳一下。",
                    "interaction": {"audience": "other", "action": "listen", "output": "silent"}}]
                current["previous_turn"] = {"turnId": "prior", "text": "都戳一下，每人戳一下。", "source": "input-audio-transcript"}
            router.set_turn_context(**current)
            result = await router.classify(text, [])
            row = {"name": name, "text": text, "expected": expected, "result": result,
                "passed": result.get("status") == "classified" and (result.get("interaction") or {}).get("action") == expected}
            report["authored"].append(row)
            save()
            print(json.dumps({"name": name, "passed": row["passed"], "action": (result.get("interaction") or {}).get("action"), "ms": result["latencyMs"]}), flush=True)
        if egolife:
            for name in ("DAY1_A1_JAKE_11100000.mp4", "DAY1_A1_JAKE_11110000.mp4", "DAY1_A1_JAKE_11120000.mp4"):
                source = ROOT / "data_real/egolife/A1_JAKE_DAY1" / name
                pcm, frame = decode(source, seconds=18)
                text = await provider.transcribe(pcm)
                row = {"source": str(source.relative_to(ROOT)), "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "offsetSeconds": 0, "durationSeconds": len(pcm)/32000, "transcript": text, "asr": audit[-1],
                    "groundTruthAddressee": None, "decisions": []}
                # Compare actual deployment ambiguity with explicit experimental framing.
                # Never infer the latter automatically from voice identity.
                for framing in ("unlabelled-microphone", "known-recorded-human-meeting"):
                    messages = [] if framing == "unlabelled-microphone" else [{"role": "user",
                        "text": "接下来是录制的人类会议，没有人在对助手提问。请只观察，不要插话；我叫助手时再回答。"}]
                    router.set_turn_context(**{**context, "recent_messages": messages})
                    result = await router.classify(text, [])
                    row["decisions"].append({"framing": framing, "result": result})
                report["egolife"].append(row)
                save()
                print(json.dumps({"source": name, "characters": len(text), "actions": [(x["result"].get("interaction") or {}) for x in row["decisions"]]}, ensure_ascii=True), flush=True)
        report["authoredPassed"] = sum(row["passed"] for row in report["authored"])
        report["authoredTotal"] = len(CASES)
    finally:
        save()
        await provider.close()
        await router.aclose()
    return report["authoredPassed"] == report["authoredTotal"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--egolife", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output, args.egolife)) else 1)
