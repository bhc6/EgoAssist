"""Real model recovery from a durable queued source after process recreation."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from egoassist._env import load_env
from egoassist.memory.recovery import MemoryRecovery, recovery_jobs
from egoassist.memory.temporal import ClockContext
from egoassist.memory.timeline import TimelineMemory
from egoassist.realtime.context_harness import SessionJournal


async def verify(output):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "providerMock": False, "failureInjected": "durable running job restored as queued",
              "hardwareMeasured": False}
    timeline = TimelineMemory(output / "timeline.sqlite3")
    journal = SessionJournal(output / "recovery.jsonl", session_id="recovery")
    policy = lambda *args: {"components": {"timeline": {"preference_enabled": True, "episodic_enabled": True}}}
    key = os.environ["QWEN_API_KEY"]
    original = MemoryRecovery(key, timeline, policy)
    text = "这件事替我留个记录：今天没有吃过药，绿色实验记录本由周岳保管。"
    clock = ClockContext.current("Asia/Shanghai", now_utc=datetime(2026, 9, 8, 23, tzinfo=timezone.utc))
    source = journal.append("message", {"message": {"role": "user", "content": text}})
    original.submit(journal, user_id="test", turn_id="source-turn", text=text, source_id=source["id"], clock=clock.prompt_payload())
    original.update(journal, recovery_jobs(journal)["source-turn"], status="running")
    await original.close()
    worker = MemoryRecovery(key, timeline, policy)
    worker.recover(journal)
    started = time.perf_counter()
    try:
        await worker.execute(journal, recovery_jobs(journal)["source-turn"])
        row = recovery_jobs(journal)["source-turn"]
        assert row["status"] == "completed", row.get("error")
        cards = timeline.cards(user_id="test", scope="participant") + timeline.cards(user_id="test", scope="case", case_id="recovery")
        assert cards and any("没有" in card["text"] for card in cards)
        assert all(card["recordedAt"].startswith("2026-09-08") for card in cards)
        count = len(cards)
        await worker.execute(journal, row)
        assert len(timeline.cards(user_id="test", scope="participant") + timeline.cards(user_id="test", scope="case", case_id="recovery")) == count
        report.update(passed=True, wallMs=round((time.perf_counter()-started)*1000,1), cards=cards, job=row)
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        await worker.close()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({"passed": report["passed"], "error": report.get("error")}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(0 if asyncio.run(verify(parser.parse_args().output)) else 1)
