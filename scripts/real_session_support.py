# Shared real-provider acceptance helpers: no mocks.
import asyncio
import json
import time
from uuid import uuid4
from aiohttp import WSMsgType


def journal(output, sid):
    path = next(output.glob(f"users/*/sessions/{sid}.jsonl"))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def turn(ws, output, sid, transcript):
    turn_id = uuid4().hex
    result = {"input": transcript, "turnId": turn_id, "audioPackets": 0}
    started = time.perf_counter()
    await ws.send_json({"type": "control", "transcript": transcript, "clientRequestId": turn_id})
    async with asyncio.timeout(45):
        while True:
            if result.get("transcript") and result["audioPackets"]:
                rows = journal(output, sid)
                terminal = [r for r in rows if r.get("eventType") == "response.finished"
                            and r.get("responseId") == result.get("responseId")
                            and r.get("payload", {}).get("origin", {}).get("turnId") == turn_id]
                if terminal:
                    result["terminal"] = terminal[-1]["payload"]
                    assert result["terminal"].get("status") == "response.done"
                    assert not result["terminal"].get("interrupted")
                    result["finishedMs"] = round((time.perf_counter() - started) * 1000, 1)
                    return result
            try:
                message = await ws.receive(timeout=0.2)
            except asyncio.TimeoutError:
                continue
            assert message.type == WSMsgType.TEXT, "WebSocket closed"
            payload = json.loads(message.data)
            kind = payload.get("type")
            if kind in {"error", "provider_unavailable"}:
                raise RuntimeError(str(payload)[:400])
            if payload.get("origin", {}).get("turnId") != turn_id:
                continue
            if kind in {"audio", "assistant_transcript"}:
                response_id = payload.get("responseId")
                assert response_id, "Provider output missing responseId"
                # A turn may include text-only subtask responses. This helper
                # accepts only the audio response used by its dialogue callers.
                if payload.get("purpose") != "conversation":
                    continue
                result.setdefault("responseId", response_id)
                assert result["responseId"] == response_id, "Audio and transcript came from different responses"
            if kind == "audio" and payload.get("audio"):
                result.setdefault("firstAudioMs", round((time.perf_counter() - started) * 1000, 1))
                result["audioPackets"] += 1
            if kind == "assistant_transcript":
                result.setdefault("firstTextMs", round((time.perf_counter() - started) * 1000, 1))
                if payload.get("done") and payload.get("text"):
                    result["transcript"] = payload["text"]
