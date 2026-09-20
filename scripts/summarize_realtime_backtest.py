"""Aggregate recorded backend and application results without new model calls."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main(args):
    root = Path(__file__).resolve().parents[1]
    core = read(args.core / "summary.json")
    report = {"coreDirectory": str(args.core), "core": core, "voice": {}, "lifecycle": {},
              "cost": None, "hardwareMeasured": False, "llmJudge": False,
              "independentHoldout": False, "codeSha256": {}}
    for backend in ("omni", "modular"):
        voice = read(args.artifacts / f"realtime-backtest-voice-{backend}-20260914" / "acceptance.json")
        audio = voice["audio"]
        report["voice"][backend] = {
            "passed": voice["passed"], "semanticPassed": sum(r.get("passed", False) for r in voice["semantic"]),
            "audioPassed": sum(r["passed"] for r in audio), "audioPlanned": 3,
            "stopEventMedianMs": statistics.median(r["audioSendToPlaybackStopMs"] for r in audio) if audio else None,
            "semanticControlMedianMs": statistics.median(r["controlMs"] for r in audio) if audio else None,
            "fixtures": [r["fixture"]["pcmSha256"] for r in audio],
            "perInstruction": [{k: r.get(k) for k in ("text", "passed", "audioSendToPlaybackStopMs", "controlMs", "routerMs")} for r in audio],
        }
        lifecycle = read(args.artifacts / f"realtime-backtest-lifecycle-{backend}-20260914" / "acceptance.json")
        report["lifecycle"][backend] = {
            "passed": lifecycle["passed"], "error": lifecycle.get("error"),
            "turns": [{"input": t["input"], "passed": t["passed"],
                       "firstAudioMs": t.get("firstAudioMs"), "task": t.get("observedState", {}).get("task")}
                      for t in lifecycle["turns"]],
            "restores": lifecycle["restores"],
            "lastDecision": {k: lifecycle["routing"][-1].get(k) for k in ("model", "status", "taskControl", "interaction")} if lifecycle.get("routing") else None,
        }
    report["voiceFixturesIdentical"] = report["voice"]["omni"]["fixtures"] == report["voice"]["modular"]["fixtures"]
    for name in ("scripts/backtest_realtime_modes.py", "scripts/verify_real_voice_stop.py",
                 "scripts/verify_real_skill_lifecycle.py", "egoassist/realtime/modular.py", "egoassist/realtime/omni.py"):
        report["codeSha256"][name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "voiceFixturesIdentical": report["voiceFixturesIdentical"],
                      "lifecycle": {k: v["passed"] for k, v in report["lifecycle"].items()}}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
