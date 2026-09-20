"""Measure actual session state on an isolated copy without model calls."""
import argparse
import asyncio
import inspect
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def verify(args):
    from egoassist import BUILTIN_SKILLS_ROOT
    from egoassist.application.composition import create_app
    import egoassist.application.state as state_module
    from egoassist.realtime.scene import SharedScene

    args.output.mkdir(parents=True, exist_ok=False)
    runtime = args.output / "runtime"
    runtime.mkdir()
    for name in ("identity.sqlite3", "timeline.sqlite3", "product.sqlite3"):
        source = sqlite3.connect(f"file:{(args.source / name).as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(runtime / name)
        source.backup(target)
        target.close()
        source.close()
    relative = Path("users") / args.user / "sessions" / (args.session + ".jsonl")
    (runtime / relative).parent.mkdir(parents=True)
    shutil.copy2(args.source / relative, runtime / relative)
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(runtime / "runtime.json"), "qwen3.5-omni-flash-realtime", "Ethan")
    handler = next(route.handler for route in app.router.routes() if route.resource.canonical == "/api/state")
    user_key = inspect.getclosurevars(handler).nonlocals["user_key"]

    class Request(dict):
        query = {"session": args.session}

    request = Request({user_key: {"id": args.user}})
    compact = state_module.compact_audit_payload
    compact_work = state_module.compact_work_item
    scene_snapshot = SharedScene.snapshot
    kinds = {"memory.recovery.job", "memory.recovery.result", "interaction.classification_retry"}
    state_module.compact_audit_payload = lambda kind, payload: dict(payload) if kind in kinds else compact(kind, payload)
    state_module.compact_work_item = lambda row: row
    def old_scene_snapshot(scene):
        result = scene_snapshot(scene)
        result["utterances"] = [{**row, "avAssociation": scene.association(row)} for row in scene.turns.values()]
        return result
    SharedScene.snapshot = old_scene_snapshot
    try:
        before = await handler(request)
    finally:
        state_module.compact_audit_payload = compact
        state_module.compact_work_item = compact_work
        SharedScene.snapshot = scene_snapshot
    after = await handler(request)
    before_data, after_data = json.loads(before.body), json.loads(after.body)
    assert before_data.keys() == after_data.keys()
    assert len(before_data["events"]) == len(after_data["events"])
    for before_turn, after_turn in zip(before_data["workLedger"]["turnInbox"], after_data["workLedger"]["turnInbox"]):
        for field in ("turnId", "text", "status", "answer", "delivery"):
            assert before_turn.get(field) == after_turn.get(field)
    assert len(after_data["sharedScene"]["utterances"]) <= 100
    assert [r["turnId"] for r in after_data["sharedScene"]["utterances"]] == [r["turnId"] for r in before_data["sharedScene"]["utterances"][-100:]]
    assert before_data["episodicMemory"] == after_data["episodicMemory"]
    report = {"session": args.session, "beforeStateBytes": len(before.body), "afterStateBytes": len(after.body),
              "reductionPercent": round((1 - len(after.body) / len(before.body)) * 100, 2),
              "eventCountPreserved": len(after_data["events"]), "turnTextAnswersAndStatusPreserved": True,
              "memoryStatePreserved": True, "modelCalls": 0, "sourceRuntimeModified": False,
              "scope": "isolated copy, state projection before/after; no server startup workers",
              "largestSectionsAfter": sorted([(k, len(json.dumps(v).encode())) for k, v in after_data.items()], key=lambda x: -x[1])[:8]}
    (args.output / "state-verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--session", required=True)
    asyncio.run(verify(parser.parse_args()))
