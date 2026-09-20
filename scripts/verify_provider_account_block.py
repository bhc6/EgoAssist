"""Probe account failure once and verify subsequent retries stay local."""
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from egoassist._env import load_env
from egoassist.realtime.semantic import SemanticTurnRouter
from egoassist.realtime.omni import OmniBrowserBridge


async def verify(output):
    load_env(str(ROOT / ".env"))
    key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    router = SemanticTurnRouter(key, timeout_seconds=10)
    report = {"answerGenerationTested": False, "accountRecoveryTested": False}
    try:
        first = await router.classify("hello", [])
        report["router"] = {"status": first["status"], "providerError": first.get("providerError")}
        if (first.get("providerError") or {}).get("accountBlocked"):
            second = await router.classify("hello again", [])
            report["router"]["secondCallStayedLocal"] = second["requestMetrics"].get("circuitOpen", False)
            assert report["router"]["secondCallStayedLocal"]
    finally:
        await router.aclose()
    os.environ["EGOASSIST_REALTIME_BACKEND"] = "omni"
    events = []

    async def emit(event):
        events.append(event)

    bridge = OmniBrowserBridge(key, "qwen3.5-omni-flash-realtime", "Ethan", "Reply only when asked.", emit, lambda text: None)
    try:
        try:
            await asyncio.wait_for(bridge.connect(), timeout=20)
        except Exception as exc:
            report["connectException"] = type(exc).__name__
        await asyncio.sleep(3)
        generation = bridge._dialogue_generation
        if bridge._blocked_failure is not None:
            for _ in range(3):
                bridge._schedule_reconnect("verification-denied-retry")
            await asyncio.sleep(2)
            assert bridge._dialogue_generation == generation
            report["omni"] = {"providerError": bridge._blocked_failure.public(),
                              "reconnectSuppressed": True, "generation": generation,
                              "unavailableEventSent": any(e.get("type") == "provider_unavailable" for e in events)}
            assert report["omni"]["unavailableEventSent"]
        else:
            report["omni"] = {"connected": bridge.connected, "reconnectSuppressed": None,
                              "note": "No account rejection observed; not a circuit-breaker validation"}
    finally:
        await bridge.close()
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(verify(Path(sys.argv[1])))
