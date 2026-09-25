"""Real-provider controls probe with scoped targets; does not execute actions."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from egoassist.realtime.decisions_router import create_turn_router
from egoassist.realtime.skills import SkillRegistry


CASES = [
    ("cancel-older", "助手，取消乘法那个旧问题的答案，光合作用的问题保留。", {"interaction.action": "cancel", "interaction.targetTurnId": "older", "interaction.output": "silent"}),
    ("cancel-unavailable", "助手，取消我昨天那个关于月球的问题。", {"interaction.action": "uncertain", "interaction.targetTurnId": None, "interaction.output": "silent"}),
    ("quoted-cancel", "助手，他说‘取消乘法那个问题’，这句话是什么意思？", {"interaction.action": "reply", "interaction.targetTurnId": None}),
    ("resume-text", "助手，把乘法那个答案继续整理好，只放在屏幕上不要读出来。", {"interaction.action": "resume", "interaction.targetTurnId": "older", "interaction.output": "text"}),
    ("resume-newer", "助手，继续给我讲光合作用。", {"interaction.action": "resume", "interaction.targetTurnId": "newer", "interaction.output": "speech"}),
    ("pause-work", "助手，先暂停光合作用那个答案的整理工作。", {"interaction.action": "pause", "interaction.targetTurnId": "newer", "interaction.output": "silent"}),
    ("exit-skill", "助手，退出当前做菜指导。", {"skillControl": "deactivate", "selectedSkill": None}),
    ("quiet-preserve", "助手，九乘九是多少？", {"interaction.action": "reply", "interaction.quiet": None}),
    ("quiet-clear", "助手，现在解除静默限制，可以正常主动说话了。", {"interaction.quiet.mode": "clear"}),
]


def field(value, path):
    for name in path.split("."):
        value = value.get(name) if isinstance(value, dict) else None
    return value


async def run(output):
    load_env()
    output.mkdir(parents=True, exist_ok=False)
    router = create_turn_router(os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY"), backend="openrouter-decisions")
    catalog = SkillRegistry(str(BUILTIN_SKILLS_ROOT)).routing_catalog()
    context = {"clock_context": None, "memory_draft": None,
        "answers": [{"turnId": "older", "text": "七乘八及乘法原理", "status": "running"},
                    {"turnId": "newer", "text": "解释光合作用", "status": "paused"}],
        "previous_answer": {"turnId": "newer", "text": "解释光合作用", "status": "paused"},
        "scene": {"speaker": {"status": "unknown", "wearer": False}},
        "output_permission": {"quiet": False, "paused": True}}
    paths = [Path(__file__), ROOT / "egoassist/prompts/decisions_router.json", ROOT / "egoassist/realtime/decisions_router.py"]
    report = {"scope": "Authored development controls, frozen before calls; no executed cancellation or skill activation.",
        "cases": CASES, "context": context, "catalog": catalog,
        "hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}, "runs": []}
    (output / "plan.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        for name, text, expected in CASES:
            router.set_turn_context(**{**context, "output_permission": {
                "quiet": name.startswith("quiet-"), "allowReplies": True, "paused": True}})
            result = await router.classify(text, catalog)
            checks = {key: field(result, key) == value for key, value in expected.items()}
            checks.update(classified=result["status"] == "classified", jevOnly=not result["requestMetrics"].get("structuredReason"),
                          noMemoryWrite=not result.get("memoryAtoms") and not result.get("memoryDraft"))
            report["runs"].append({"case": name, "checks": checks, "passed": all(checks.values()), "result": result})
            (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": name, "passed": all(checks.values()), "checks": checks, "ms": result["latencyMs"]}), flush=True)
    finally:
        await router.aclose()
    return 0 if all(row["passed"] for row in report["runs"]) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    raise SystemExit(asyncio.run(run(parser.parse_args().output)))
