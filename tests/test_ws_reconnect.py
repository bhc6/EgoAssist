"""A reconnect that arrives during teardown must be served, not rejected.

The browser reconnects as soon as it sees the socket close, which is typically
before the server has finished the previous socket's teardown (bridge close,
tool confirmations, media flush). That window used to answer HTTP 409; the
browser cannot see the status, so it burned its short retry budget and gave up.
Observed in .egoassist/server.log: every 409 was followed within milliseconds
by the previous socket's 101 access line.
"""

import asyncio
import time

import pytest
from aiohttp.client_exceptions import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.application.ownership import CaseRuntimeOwner
from egoassist.application.realtime_connection import RealtimeConnection
from run_egoassist import create_app

from tests.test_realtime_api import _recv, install_queue_test_bridge, register_client


async def initial_messages(ws):
    ready = await _recv(ws)
    state = await _recv(ws)
    assert ready["type"] == "ready" and state["type"] == "state"
    return ready, state["state"]


class _FakeOwner:
    """Release state exactly as a fresh CaseRuntimeOwner has it."""

    def __init__(self):
        defaults = CaseRuntimeOwner.__dataclass_fields__
        self.socket_token = None
        self.transitioning = False
        self.releasing = False
        # Same factory as the real owner, so the event starts in the real state.
        self.released = defaults["released"].default_factory()


class _Gate:
    """Just the release gate, without building the 50-field server dataclass."""

    await_socket_release = RealtimeConnection.await_socket_release
    _session_free = staticmethod(RealtimeConnection._session_free)
    RECONNECT_RELEASE_TIMEOUT = 15.0
    RECONNECT_POLL_INTERVAL = RealtimeConnection.RECONNECT_POLL_INTERVAL

    def __init__(self, timeout=None):
        if timeout is not None:
            self.RECONNECT_RELEASE_TIMEOUT = timeout


def test_gate_accepts_when_no_socket_holds_the_session():
    async def scenario():
        assert await _Gate().await_socket_release(_FakeOwner()) is True

    asyncio.run(scenario())


def test_gate_refuses_a_live_second_socket_immediately():
    """Single-writer must not be weakened by the wait."""

    async def scenario():
        owner = _FakeOwner()
        owner.socket_token = object()  # a live socket, not releasing
        started = time.monotonic()
        assert await _Gate().await_socket_release(owner) is False
        # Refused without waiting: far below the 15 s wait budget. The bound is
        # generous so a loaded CI machine does not make this flaky.
        assert time.monotonic() - started < 2.0, "a live socket must be refused, not waited on"

    asyncio.run(scenario())


def test_gate_waits_through_a_transition_without_a_release_signal():
    """A skill/task switch frees the session without setting ``released``.

    The gate must notice by re-checking, not sit out its whole timeout.
    """

    async def scenario():
        owner = _FakeOwner()
        owner.transitioning = True  # exclusive_case_transition in progress

        async def finish_transition():
            await asyncio.sleep(0.3)
            owner.transitioning = False  # note: released is NOT set here

        task = asyncio.create_task(finish_transition())
        started = time.monotonic()
        assert await _Gate().await_socket_release(owner) is True
        assert time.monotonic() - started < 5.0, "must not wait the full 15 s timeout"
        await task

    asyncio.run(scenario())


def test_gate_waits_for_a_releasing_socket_then_accepts():
    async def scenario():
        owner = _FakeOwner()
        owner.socket_token = object()
        owner.releasing = True
        owner.transitioning = True
        owner.released.clear()

        async def finish_release():
            await asyncio.sleep(0.2)
            owner.socket_token = None
            owner.transitioning = False
            owner.releasing = False
            owner.released.set()

        releaser = asyncio.create_task(finish_release())
        assert await _Gate().await_socket_release(owner) is True
        await releaser

    asyncio.run(scenario())


def test_gate_times_out_on_a_stuck_release():
    async def scenario():
        owner = _FakeOwner()
        owner.socket_token = object()
        owner.releasing = True
        owner.released.clear()
        started = time.monotonic()
        assert await _Gate(timeout=0.2).await_socket_release(owner) is False
        # It did wait (not an instant refusal) and it did give up.
        assert 0.15 <= time.monotonic() - started < 5.0

    asyncio.run(scenario())


def test_reconnect_after_close_is_served(tmp_path, monkeypatch):
    """End-to-end: close and immediately reconnect must not return 409."""
    install_queue_test_bridge(monkeypatch)

    async def scenario():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path / "memory.json"),
                         "test-realtime", "Ethan")
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            session_id = (await (await client.post(
                "/api/sessions", json={"label": "reconnect"})).json())["session"]["id"]

            first = await client.ws_connect(f"/ws/realtime?session={session_id}")
            await initial_messages(first)
            await first.close()

            second = await client.ws_connect(f"/ws/realtime?session={session_id}")
            ready, _ = await initial_messages(second)
            assert ready["contextSessionId"] == session_id
            await second.close()

    asyncio.run(scenario())


def test_second_live_socket_is_still_refused(tmp_path, monkeypatch):
    install_queue_test_bridge(monkeypatch)

    async def scenario():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path / "memory.json"),
                         "test-realtime", "Ethan")
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            session_id = (await (await client.post(
                "/api/sessions", json={"label": "single-writer"})).json())["session"]["id"]
            first = await client.ws_connect(f"/ws/realtime?session={session_id}")
            await initial_messages(first)
            with pytest.raises(WSServerHandshakeError) as conflict:
                await client.ws_connect(f"/ws/realtime?session={session_id}")
            assert conflict.value.status == 409
            await first.close()

    asyncio.run(scenario())


def test_failed_connect_releases_the_session_for_retry(tmp_path, monkeypatch):
    """A rejected connect must wake the next one instead of making it wait."""
    install_queue_test_bridge(monkeypatch)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    async def scenario():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path / "memory.json"),
                         "test-realtime", "Ethan")
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            session_id = (await (await client.post(
                "/api/sessions", json={"label": "no-key"})).json())["session"]["id"]
            failed = await client.ws_connect(f"/ws/realtime?session={session_id}")
            assert (await failed.receive_json())["type"] == "error"
            await failed.close()

            monkeypatch.setenv("QWEN_API_KEY", "test-key")
            started = time.monotonic()
            retried = await client.ws_connect(f"/ws/realtime?session={session_id}")
            assert (await retried.receive_json())["type"] == "ready"
            # Well under the 15 s release wait; generous for slow CI machines.
            assert time.monotonic() - started < 8.0, "retry should not wait for a stale release"
            await retried.close()

    asyncio.run(scenario())
