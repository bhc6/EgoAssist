"""Record provider protocol behavior for text input with and without server VAD."""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import aiohttp

from egoassist._env import load_env


async def probe(output):
    output.mkdir(parents=True, exist_ok=False)
    key = os.getenv("QWEN_API_KEY") or os.environ["DASHSCOPE_API_KEY"]
    results = []
    async with aiohttp.ClientSession() as http:
        for name, vad, silence in [("server-vad-text", True, False), ("manual-text", False, False), ("manual-silence-text", False, True)]:
            started = time.monotonic()
            row = {"name": name, "model": "qwen3.5-omni-flash-realtime", "events": [], "sent": []}
            results.append(row)
            async with http.ws_connect(
                "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=" + row["model"],
                headers={"Authorization": "Bearer " + key},
            ) as ws:
                async def send(kind, **body):
                    payload = {"event_id": "event_" + uuid4().hex, "type": kind, **body}
                    row["sent"].append({**payload, **({"audio": "synthetic silence"} if "audio" in payload else {})})
                    await ws.send_json(payload)

                async def receive():
                    msg = await asyncio.wait_for(ws.receive(), 10)
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        return {"type": "closed", "value": str(msg.type)}
                    payload = json.loads(msg.data)
                    saved = dict(payload)
                    if payload.get("type") == "response.audio.delta":
                        saved["audioBytes"] = len(base64.b64decode(saved.pop("delta", "")))
                    row["events"].append({"elapsedMs": round((time.monotonic() - started) * 1000), **saved})
                    return payload

                await receive()
                await send("session.update", session={
                    "modalities": ["text", "audio"], "voice": "Ethan",
                    "input_audio_format": "pcm", "output_audio_format": "pcm",
                    "instructions": "Answer the user's question briefly.",
                    "turn_detection": {"type": "server_vad", "create_response": False, "interrupt_response": False} if vad else None,
                })
                await receive()
                if silence:
                    await send("input_audio_buffer.append", audio=base64.b64encode(bytes(6400)).decode())
                    await send("input_audio_buffer.commit")
                    while (await receive())["type"] != "conversation.item.input_audio_transcription.completed":
                        pass
                await send("conversation.item.create", item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "What is two plus three?"}]})
                while (await receive())["type"] != "conversation.item.created":
                    pass
                await send("response.create", response={"modalities": ["text", "audio"], "instructions": "Answer: what is two plus three?"})
                try:
                    async with asyncio.timeout(12):
                        while True:
                            payload = await receive()
                            if payload["type"] in {"response.done", "error", "closed"}:
                                break
                except TimeoutError:
                    row["timeout"] = True
            (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"name": name, "events": [x["type"] for x in row["events"]], "timeout": row.get("timeout", False)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    load_env()
    asyncio.run(probe(args.output))
