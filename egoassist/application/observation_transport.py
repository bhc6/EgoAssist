"""Publish accepted observation state after its synchronous commit is complete."""


async def deliver_observation(observer, captured, raw, *, ws, state):
    result = await observer.session_perception_result(captured, raw)
    if not ws.closed:
        await ws.send_json({'type': 'state', 'state': state()})
        if result.get('ambient') is not None:
            await ws.send_json({'type': 'ambient', 'ambient': result['ambient']})
    return result
