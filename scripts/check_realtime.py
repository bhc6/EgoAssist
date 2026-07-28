"""Verify that the local demo can establish a real Omni session."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
from collections import Counter

import aiohttp
from PIL import Image


async def check(
    url: str,
    exercise_seconds: float = 0,
    stress_controls: bool = False,
    stress_media_gap: bool = False,
) -> bool:
    events = []
    errors = []
    event_counts: Counter[str] = Counter()
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, timeout=45) as websocket:
            for _ in range(6):
                message = await asyncio.wait_for(websocket.receive(), 45)
                if message.type != aiohttp.WSMsgType.TEXT:
                    events.append(str(message.type))
                    continue
                payload = json.loads(message.data)
                events.append(payload.get("type"))
                event_counts[payload.get("type", "unknown")] += 1
                if payload.get("type") == "error":
                    errors.append(str(payload.get("message", ""))[:500])
                if payload.get("type") in {"ready", "error"}:
                    break
            if "ready" in events and exercise_seconds > 0:
                await websocket.send_json({
                    "type": "media_config",
                    "playbackCapture": False,
                    "audio": {
                        "channelCount": 1,
                        "sampleRate": 48000,
                        "echoCancellation": True,
                        "noiseSuppression": True,
                        "autoGainControl": True,
                    },
                })
                image_buffer = io.BytesIO()
                Image.new("RGB", (160, 120), (92, 112, 96)).save(image_buffer, "JPEG")
                image = base64.b64encode(image_buffer.getvalue()).decode("ascii")
                silent_audio = base64.b64encode(bytes(16000)).decode("ascii")
                loop = asyncio.get_running_loop()
                deadline = loop.time() + exercise_seconds
                next_frame = loop.time()
                next_control = loop.time() + 2.0
                control_index = 0
                next_media_gap = loop.time() + 3.0
                media_gap_until = 0.0
                delayed_video_at = 0.0
                delayed_video_sent = False
                while loop.time() < deadline:
                    now = loop.time()
                    if stress_media_gap and now >= next_media_gap:
                        await websocket.send_json({"type": "audio", "audio": silent_audio})
                        delayed_video_at = now + 0.8
                        media_gap_until = now + 1.0
                        delayed_video_sent = False
                        next_media_gap = float("inf")
                    if media_gap_until and not delayed_video_sent and now >= delayed_video_at:
                        await websocket.send_json({"type": "video", "image": image})
                        delayed_video_sent = True
                    in_media_gap = bool(media_gap_until and now < media_gap_until)
                    if not in_media_gap and now >= next_frame:
                        await websocket.send_json({"type": "video", "image": image})
                        await websocket.send_json({"type": "audio", "audio": silent_audio})
                        next_frame = now + 0.5
                        media_gap_until = 0.0
                    if stress_controls and now >= next_control:
                        transcript = ("再说一遍", "怎么做")[control_index % 2]
                        await websocket.send_json({"type": "control", "transcript": transcript})
                        control_index += 1
                        next_control += 2.5
                    try:
                        message = await asyncio.wait_for(websocket.receive(), 0.2)
                    except asyncio.TimeoutError:
                        continue
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    payload = json.loads(message.data)
                    event_type = payload.get("type", "unknown")
                    event_counts[event_type] += 1
                    if event_type == "error":
                        errors.append(str(payload.get("message", ""))[:500])
    connected = "ready" in events
    print(json.dumps({
        "events": events,
        "eventCounts": event_counts,
        "connected": connected,
        "stressControls": stress_controls,
        "stressMediaGap": stress_media_gap,
        "errors": errors,
    }, ensure_ascii=False))
    return connected and not errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8770/ws/realtime")
    parser.add_argument("--exercise-seconds", type=float, default=0)
    parser.add_argument("--stress-controls", action="store_true")
    parser.add_argument("--stress-media-gap", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(check(
        args.url, args.exercise_seconds, args.stress_controls, args.stress_media_gap,
    )) else 1)


if __name__ == "__main__":
    main()
