"""Verify one bundled example Skill/task route on a live realtime session."""

from __future__ import annotations

import asyncio
import json

import aiohttp


async def receive_state(websocket, predicate, errors, timeout=30):
    async with asyncio.timeout(timeout):
        async for message in websocket:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            if payload.get("type") == "error":
                errors.append(payload.get("message", "unknown error"))
            if payload.get("type") == "state" and predicate(payload["state"]):
                return payload["state"]
    raise TimeoutError("expected routed state was not observed")


async def check(base_url: str) -> bool:
    errors = []
    async with aiohttp.ClientSession() as session:
        await session.post(base_url + "/api/task/reset", json={})
        async with session.ws_connect(base_url + "/ws/realtime", timeout=45) as websocket:
            initial = await receive_state(
                websocket,
                lambda state: state["activeSkill"] is None and state["task"] is None,
                errors,
            )
            await websocket.send_json({"type": "control", "transcript": "我想做菜，但还没决定做什么"})
            skill_state = await receive_state(
                websocket,
                lambda state: state["activeSkill"] and state["activeSkill"]["name"] == "kitchen-coach" and state["task"] is None,
                errors,
            )
            await websocket.send_json({"type": "control", "transcript": "请指导我做水果沙拉"})
            task_state = await receive_state(
                websocket,
                lambda state: state["task"] and state["task"]["task"]["id"] == "fruit-salad",
                errors,
            )
            result = {
                "sameWebSocket": not websocket.closed,
                "initial": {"skill": initial["activeSkill"], "task": initial["task"]},
                "skillOnly": {
                    "skill": skill_state["activeSkill"]["name"],
                    "task": skill_state["task"],
                    "route": skill_state["intentRouting"]["status"],
                },
                "taskSelected": {
                    "skill": task_state["activeSkill"]["name"],
                    "task": task_state["task"]["task"]["id"],
                    "episodes": task_state["episodicMemory"]["count"],
                },
                "errors": errors,
            }
            print(json.dumps(result, ensure_ascii=False))
            return result["sameWebSocket"] and not errors


def main() -> None:
    raise SystemExit(0 if asyncio.run(check("http://127.0.0.1:8770")) else 1)


if __name__ == "__main__":
    main()
