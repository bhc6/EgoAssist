"""Verify that the local demo can establish a real Omni session."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
from collections import Counter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
from PIL import Image


async def check(
    url: str,
    exercise_seconds: float = 0,
    stress_controls: bool = False,
    stress_media_gap: bool = False,
    username: str = "realtime-check",
    password: str = "realtime-check-pass",
    transcript: str = "",
    expected_skill: str = "",
    case_label: str = "realtime-provider-check",
    result_timeout: float = 30,
    interrupt_transcript: str = "",
    interrupt_delay: float = 0.8,
) -> bool:
    events = []
    errors = []
    event_counts: Counter[str] = Counter()
    provider_connected = False
    parts = urlsplit(url)
    auth_scheme = "https" if parts.scheme in {"https", "wss"} else "http"
    auth_root = f"{auth_scheme}://{parts.netloc}"
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        login = await session.post(
            auth_root + "/api/auth/login",
            json={"username": username, "password": password},
        )
        if login.status == 400:
            login = await session.post(
                auth_root + "/api/auth/register",
                json={"username": username, "password": password},
            )
        if login.status != 200:
            detail = (await login.text())[:500]
            print(json.dumps({"connected": False, "authStatus": login.status, "errors": [detail]}))
            return False
        context_session_id = ""
        if transcript:
            created = await session.post(
                auth_root + "/api/sessions",
                json={"label": case_label},
            )
            if created.status != 201:
                detail = (await created.text())[:500]
                print(json.dumps({"connected": False, "sessionStatus": created.status, "errors": [detail]}))
                return False
            context_session_id = str((await created.json())["session"]["id"])
            ws_parts = urlsplit(url)
            query = dict(parse_qsl(ws_parts.query, keep_blank_values=True))
            query["session"] = context_session_id
            url = urlunsplit((
                ws_parts.scheme, ws_parts.netloc, ws_parts.path,
                urlencode(query), ws_parts.fragment,
            ))
        latest_state = {}
        assistant_transcript = ""
        transcript_done = False
        async with session.ws_connect(url, timeout=45) as websocket:
            for _ in range(6):
                message = await asyncio.wait_for(websocket.receive(), 45)
                if message.type != aiohttp.WSMsgType.TEXT:
                    events.append(str(message.type))
                    continue
                payload = json.loads(message.data)
                events.append(payload.get("type"))
                event_counts[payload.get("type", "unknown")] += 1
                if payload.get("type") == "ready":
                    provider_connected = bool((payload.get("lanes") or {}).get("dialogue", {}).get("connected"))
                    if not provider_connected:
                        errors.append("ready received without a connected dialogue provider")
                if payload.get("type") == "provider_unavailable":
                    errors.append(str(payload.get("reason", "provider unavailable"))[:500])
                if payload.get("type") == "error":
                    errors.append(str(payload.get("message", ""))[:500])
                if payload.get("type") == "state":
                    latest_state = payload.get("state") or {}
                if payload.get("type") in {"ready", "error"}:
                    break
            if provider_connected and transcript:
                await websocket.send_json({"type": "control", "transcript": transcript})
                loop = asyncio.get_running_loop()
                deadline = loop.time() + result_timeout
                interrupt_at = loop.time() + interrupt_delay
                interrupt_sent = False
                while loop.time() < deadline:
                    if interrupt_transcript and not interrupt_sent and loop.time() >= interrupt_at:
                        await websocket.send_json({"type": "control", "transcript": interrupt_transcript})
                        interrupt_sent = True
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
                        break
                    if event_type == "assistant_transcript":
                        transcript_done = bool(payload.get("done") and payload.get("text"))
                        assistant_transcript = (
                            str(payload.get("text", ""))
                            if payload.get("done")
                            else assistant_transcript + str(payload.get("text", ""))
                        )
                    if event_type == "state":
                        latest_state = payload.get("state") or {}
                    active_name = (latest_state.get("activeSkill") or {}).get("name")
                    if (transcript_done and (not expected_skill or active_name == expected_skill)
                            and (not interrupt_transcript or interrupt_sent)):
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
    connected = provider_connected
    active_skill = (latest_state.get("activeSkill") or {}).get("name")
    routing = latest_state.get("intentRouting") or {}
    print(json.dumps({
        "events": events,
        "eventCounts": event_counts,
        "connected": connected,
        "stressControls": stress_controls,
        "stressMediaGap": stress_media_gap,
        "username": username,
        "contextSessionId": context_session_id or None,
        "transcript": transcript or None,
        "activeSkill": active_skill,
        "intentRouting": {
            "method": routing.get("method"),
            "status": routing.get("status"),
            "selectedSkill": routing.get("selectedSkill"),
        },
        "assistantTranscript": assistant_transcript or None,
        "interruptTranscript": interrupt_transcript or None,
        "errors": errors,
    }, ensure_ascii=False))
    skill_ok = not expected_skill or active_skill == expected_skill
    output_ok = not transcript or transcript_done
    return connected and not errors and skill_ok and output_ok


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8770/ws/realtime")
    parser.add_argument("--exercise-seconds", type=float, default=0)
    parser.add_argument("--stress-controls", action="store_true")
    parser.add_argument("--stress-media-gap", action="store_true")
    parser.add_argument("--username", default="realtime-check")
    parser.add_argument("--transcript", default="")
    parser.add_argument("--expect-skill", default="")
    parser.add_argument("--case-label", default="realtime-provider-check")
    parser.add_argument("--result-timeout", type=float, default=30)
    parser.add_argument("--interrupt-transcript", default="")
    parser.add_argument("--interrupt-delay", type=float, default=0.8)
    parser.add_argument(
        "--password",
        default=os.getenv("EGOASSIST_CHECK_PASSWORD", "realtime-check-pass"),
    )
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(check(
        args.url, args.exercise_seconds, args.stress_controls, args.stress_media_gap,
        args.username, args.password, args.transcript, args.expect_skill,
        args.case_label, args.result_timeout, args.interrupt_transcript,
        args.interrupt_delay,
    )) else 1)


if __name__ == "__main__":
    main()
