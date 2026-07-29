"""Run a small real-provider regression matrix for the semantic turn router."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from egoassist.realtime.semantic import SemanticTurnRouter
from egoassist.realtime.skills import SkillRegistry


CASES: tuple[dict[str, Any], ...] = (
    {"name": "direct-stop", "text": "别说了", "expect": {"speechControl": "stop-speaking"}},
    {"name": "negated-stop", "text": "我不是让你别说了，请继续解释", "expect": {"speechControl": None}},
    {"name": "quoted-stop", "text": "他刚才说‘别说了’，这句话是什么意思？", "expect": {"speechControl": None}},
    {"name": "cook-task", "text": "我想做宫保鸡丁", "expect": {"selectedSkill": "kitchen-coach"}},
    {"name": "capability", "text": "请介绍一下你能做什么", "expect": {"selectedSkill": None}},
    {"name": "pause-task", "text": "把任务暂停一下，但你可以回答问题", "expect": {"taskControl": "pause-task", "speechControl": None}},
    {"name": "repeat-step", "text": "请再说一遍刚才那一步", "expect": {"taskControl": "repeat"}},
    {"name": "durable-preference", "text": "以后每次做菜都少放盐", "expect": {"preferenceIntent": "propose-durable"}},
    {"name": "memory-reference", "text": "刚才你建议的火候是什么？", "expect": {"memoryQuery": "referential"}},
)


async def run(args: argparse.Namespace) -> int:
    load_dotenv(args.env)
    api_key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("QWEN_API_KEY or DASHSCOPE_API_KEY is required")
    registry = SkillRegistry(args.skills)
    router = SemanticTurnRouter(api_key, model=args.model, timeout_seconds=args.timeout)
    failures = 0
    for case in CASES:
        result = await router.classify(case["text"], registry.summaries())
        mismatches = {
            field: {"expected": expected, "actual": result.get(field)}
            for field, expected in case["expect"].items()
            if result.get(field) != expected
        }
        failures += bool(mismatches)
        print(json.dumps({
            "case": case["name"],
            "status": result.get("status"),
            "speechControl": result.get("speechControl"),
            "taskControl": result.get("taskControl"),
            "preferenceIntent": result.get("preferenceIntent"),
            "memoryQuery": result.get("memoryQuery"),
            "selectedSkill": result.get("selectedSkill"),
            "skillStatus": result.get("skillStatus"),
            "latencyMs": result.get("latencyMs"),
            "mismatches": mismatches,
            "error": result.get("error"),
        }, ensure_ascii=False))
    print(json.dumps({"cases": len(CASES), "failures": failures}, ensure_ascii=False))
    return 1 if failures else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--skills", type=Path, default=Path("egoassist/skills"))
    parser.add_argument("--model", default=os.getenv("EGOASSIST_TURN_ROUTER_MODEL", "qwen-flash"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("EGOASSIST_TURN_ROUTER_TIMEOUT", "3.0")))
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
