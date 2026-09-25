"""Same-input full-contract development comparison; no workflow actions executed."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from egoassist.realtime.decisions_router import create_turn_router
from egoassist.realtime.skills import SkillRegistry
from verify_router_latency import CASES, assess


EXTRA = [
    ("ordinary-fact", "助手，我今天把备用眼镜放到了卧室床头柜。", "observed"),
    ("save-denied", "助手，不要把我的门牌号7316存入长期记忆，只回答收到。", "no-save"),
    ("recall-new", "助手，上周我把备用眼镜放在哪里了？", "recall"),
    ("quote-reminder", "助手，解释一下‘十分钟后提醒我站起来’这句话的语法，不要真的设置提醒。", "no-reminder"),
    ("compound-stop", "助手，别读出声音，三分钟后提醒我喝水。", "silent-reminder"),
    ("task-partial", "助手，鸡蛋已经准备好了，但番茄还没拿。", "no-confirm"),
    ("task-resume", "助手，继续刚才暂停的做菜任务。", "resume-task"),
    ("quiet-duration", "助手，接下来两分钟不要主动说话，等我问你再回答。", "quiet-duration"),
    ("text-only", "助手，请只用文字告诉我九乘九的结果，不要念出来。", "text-only"),
    ("correction-new", "助手，刚才记的钥匙位置不对，请更正为书房黄色抽屉。", "correction"),
]


def checks(result, expected):
    values = assess(result, expected)
    interaction = result.get("interaction") or {}
    atoms = result.get("memoryAtoms") or []
    intents = result.get("intents") or []
    if expected == "observed":
        values["source-memory"] = any(a.get("consent") == "observed" and a.get("scope") == "case" for a in atoms)
    elif expected == "no-save":
        values["no-explicit-save"] = not result.get("memoryDraft") and not any(a.get("consent") == "explicit" for a in atoms)
    elif expected == "no-reminder":
        values["no-reminder"] = not any(i.get("kind") == "background-reminder" for i in intents)
    elif expected == "silent-reminder":
        values["reminder-180"] = any(i.get("kind") == "background-reminder" and i.get("parameters", {}).get("delaySeconds") == 180 for i in intents)
        values["no-speech"] = interaction.get("output") in {"text", "silent"}
    elif expected == "resume-task":
        values["resume-task"] = result.get("taskControl") == "resume-task"
    elif expected == "quiet-duration":
        quiet = interaction.get("quiet") or {}
        values["bounded-quiet"] = quiet.get("mode") == "set" and quiet.get("seconds") == 120 and quiet.get("allowReplies") is True
    elif expected == "text-only":
        values["text-only"] = interaction.get("action") == "reply" and interaction.get("output") == "text"
    elif expected == "correction":
        values["replaces-target"] = any(a.get("replaces") == "key-card" and "黄色抽屉" in a.get("text", "") for a in atoms)
    return values


def summary(rows):
    result = {}
    for backend in sorted({r["backend"] for r in rows}):
        subset = [r for r in rows if r["backend"] == backend]
        result[backend] = {"count": len(subset), "passed": sum(r["passed"] for r in subset),
            "classified": sum(r["result"]["status"] == "classified" for r in subset),
            "medianMs": statistics.median(r["result"]["latencyMs"] for r in subset),
            "structuredCalls": sum(bool(r["result"]["requestMetrics"].get("structuredReason")) for r in subset),
            "componentErrors": sum(bool(r["result"].get("componentErrors")) for r in subset)}
    return result


async def run(args):
    load_env()
    args.output.mkdir(parents=True, exist_ok=False)
    catalog = SkillRegistry(str(BUILTIN_SKILLS_ROOT)).routing_catalog()
    routers = {b: create_turn_router(os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY"),
                    backend=b, model="qwen3-max", timeout_seconds=args.timeout) for b in args.backends}
    files = [Path(__file__), ROOT / "egoassist/realtime/decisions_router.py", ROOT / "egoassist/realtime/semantic.py",
             ROOT / "egoassist/core/typed_decisions.py", *sorted((ROOT / "egoassist/prompts").glob("*.json"))]
    report = {"createdAt": datetime.now(timezone.utc).isoformat(), "timeoutSeconds": args.timeout,
              "scope": "Exposed development cases and new authored contrast cases; full router assertions, no actions/ASR/hardware.",
              "cases": CASES + EXTRA, "backends": args.backends, "repeats": args.repeats,
              "hashes": {str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}, "runs": []}
    (args.output / "plan.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        for repeat in range(args.repeats):
            for index, (name, text, expected) in enumerate(CASES + EXTRA):
                for backend in (args.backends if (index + repeat) % 2 == 0 else args.backends[::-1]):
                    task = {"currentCheckpointId": "ingredients", "currentStep": {"id": "ingredients", "title": "准备食材",
                            "instruction": "准备鸡蛋、番茄、油和盐", "completion": "用户确认全部食材准备齐全", "status": "active"},
                            "paused": name == "task-resume", "completed": False} if name.startswith("task-") else None
                    cards = [{"id": "key-card", "referenceId": "m1", "kind": "state", "subject": "钥匙", "predicate": "位置",
                              "value": "玄关绿色收纳盒", "text": "钥匙在玄关绿色收纳盒", "scope": "participant"}] if name == "correction-new" else []
                    router = routers[backend]
                    router.set_turn_context(clock_context=None, memory_draft=None, active_task=task, memory_candidates=cards,
                                            scene={"speaker": {"status": "unknown", "wearer": False}},
                                            output_permission={"quiet": False, "paused": True})
                    result = await router.classify(text, catalog)
                    assertions = checks(result, expected)
                    row = {"case": name, "repeat": repeat, "text": text, "backend": backend, "expected": expected,
                           "checks": assertions, "passed": all(assertions.values()), "result": result}
                    report["runs"].append(row)
                    report["summary"] = summary(report["runs"])
                    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(json.dumps({"case": name, "backend": backend, "passed": row["passed"], "status": result["status"],
                                      "ms": result["latencyMs"], "route": result["requestMetrics"].get("decisions", {}).get("route")}), flush=True)
    finally:
        for router in routers.values(): await router.aclose()
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    return 0 if all(r["passed"] for r in report["runs"]) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--backends", nargs="+", choices=["qwen", "openrouter-decisions"], default=["qwen", "openrouter-decisions"])
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--timeout", type=float, default=4.0)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
