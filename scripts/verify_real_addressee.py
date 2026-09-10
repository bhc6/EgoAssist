"""Real semantic model checks for unknown voices, addressees and ambiguous live logs."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from egoassist._env import load_env
from egoassist.realtime.semantic import SemanticTurnRouter
from egoassist.realtime.scene import restrict_unverified_speaker

CASES = [
    ("direct-question", "助手，请告诉我七乘八等于多少。", "reply"),
    ("direct-visual", "眼镜助手，请描述我面前的桌布颜色。", "reply"),
    ("direct-quiet", "助手，我在开会，暂时别出声。", "silent"),
    ("third-party", "小李，你明天把会议纪要发给小王。", "silent"),
    ("quoted-request", "小王刚才说，助手请告诉我天气，他是在举例。", "silent"),
    ("negated-address", "我不是在问助手，小李，我是在问你会议几点开始。", "silent"),
    ("live-ambiguous", "那算了，我觉得不如开腾讯会议，然后开开电脑。明天再说。", "silent"),
    ("unknown-write", "助手，这件事替我留个记录：我今天没有吃过药。", "write"),
]


async def verify(output):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    router = SemanticTurnRouter(os.getenv("QWEN_API_KEY") or os.environ["DASHSCOPE_API_KEY"])
    report = {"mock": False, "scope": "Real semantic provider plus production unknown-speaker guard; text fixtures, not ASR or ground-truth speaker identification", "cases": []}
    try:
        for name, text, expected in CASES:
            router.set_turn_context(clock_context=None, memory_draft=None,
                                    scene={"turnId": name, "speaker": {"status": "pending", "wearer": False}})
            raw = await router.classify(text, [])
            guarded = restrict_unverified_speaker(raw)
            control = guarded.get("interaction") or {}
            passed = raw["status"] == "classified"
            if expected == "reply":
                passed = passed and control.get("audience") == "assistant" and control.get("action") == "reply" and control.get("output") == "speech"
            elif expected == "silent":
                passed = passed and control.get("output") == "silent"
            elif expected == "write":
                passed = passed and bool(guarded.get("memoryAtoms")) and control.get("audience") == "assistant"
            row = {"case": name, "text": text, "expected": expected, "passed": passed, "raw": raw, "guarded": guarded}
            report["cases"].append(row)
            print(json.dumps({"case": name, "passed": passed, "control": control, "ms": raw["latencyMs"]}), flush=True)
    finally:
        await router.aclose()
        report["passed"] = len(report["cases"]) == len(CASES) and all(c["passed"] for c in report["cases"])
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
