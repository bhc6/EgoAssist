"""Exercise real provider startup using an isolated copy of a session journal."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import time

from egoassist._env import load_env
from egoassist.core.journal import SessionJournal
from egoassist.realtime.context_harness import ContextHarness
from egoassist.realtime.omni import OmniBrowserBridge


async def verify(source: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    copied = output / "session.jsonl"
    shutil.copy2(source, copied)
    harness = ContextHarness(SessionJournal(copied))
    expected_items = len(harness.provider_replay_items())
    events, traces, gaps = [], [], []
    start = time.perf_counter()

    async def emit(payload):
        events.append({"elapsedMs": round((time.perf_counter() - start) * 1000, 1), **payload})

    async def transcript(_):
        pass

    def trace(kind, source, payload, parents, **kwargs):
        if kind in {"context.replayed", "provider.session.ready", "provider.session.unavailable"}:
            traces.append({"type": kind, "payload": payload})
        return {}

    async def heartbeat():
        previous = time.perf_counter()
        while True:
            await asyncio.sleep(0.02)
            current = time.perf_counter()
            gaps.append((current - previous) * 1000)
            previous = current

    bridge = OmniBrowserBridge(
        os.getenv("QWEN_API_KEY") or os.environ["DASHSCOPE_API_KEY"],
        "qwen3.5-omni-flash-realtime", "Ethan",
        "You are a helpful assistant. Wait for a new user request.", emit, transcript,
    )
    bridge.set_context_harness(harness)
    bridge.set_trace_sink(trace)
    monitor = asyncio.create_task(heartbeat())
    error = None
    try:
        await asyncio.wait_for(bridge.connect(), 60)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        elapsed = round((time.perf_counter() - start) * 1000, 1)
        connected = bridge.connected
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await bridge.close()
    result = {
        "scope": "real provider bridge startup with copied historical journal; no microphone or camera",
        "connected": connected, "elapsedMs": elapsed, "historyItems": expected_items,
        "maxLoopGapMs": round(max(gaps, default=0), 1), "heartbeatSamples": len(gaps),
        "events": events, "traces": traces, "error": error,
    }
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return connected and error is None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    load_env()
    raise SystemExit(0 if asyncio.run(verify(args.journal, args.output)) else 1)
