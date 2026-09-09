import asyncio
import base64
from datetime import datetime, timedelta, timezone
import io

from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

from egoassist.realtime.context_harness import SessionJournal
from egoassist.realtime.scene import SharedScene
from egoassist.realtime.work_ledger import CaseWorkLedger


def scene_with_frame(tmp_path):
    scene = SharedScene(SessionJournal(tmp_path / "scene.jsonl", session_id="scene"), "")
    out = io.BytesIO()
    Image.new("RGB", (24, 24), "red").save(out, format="JPEG")
    scene.append_frame(base64.b64encode(out.getvalue()).decode())
    scene.capture("question-one", explicit=True)
    scene.capture("question-two", explicit=True)
    scene.api_key = "unit-test-key"
    return scene


def test_focused_question_deduplicates_and_ignores_slow_scene_request(tmp_path, monkeypatch):
    import dashscope

    async def run():
        scene = scene_with_frame(tmp_path)
        calls = []
        release = asyncio.Event()

        async def answer(request):
            body = await request.json()
            calls.append(body)
            await release.wait()
            question = body["input"]["messages"][-1]["content"][-1]["text"]
            return web.json_response({"request_id": question, "output": {"choices": [
                {"message": {"content": [{"text": "Evidence for " + question}]}, "finish_reason": "stop"}]}})

        app = web.Application()
        app.router.add_post("/services/aigc/multimodal-generation/generation", answer)
        async with TestServer(app) as server:
            monkeypatch.setattr(dashscope, "base_http_api_url", str(server.make_url("")))
            # Hold the general scene lane indefinitely: foreground must remain independent.
            await scene.vision_lock.acquire()
            await scene.vision_lock.acquire()
            one = asyncio.create_task(scene.context("question-one", describe_frame=True, question="color"))
            duplicate = asyncio.create_task(scene.context("question-one", describe_frame=True, question="color"))
            two = asyncio.create_task(scene.context("question-two", describe_frame=True, question="objects"))
            async with asyncio.timeout(2):
                while len(calls) < 2:
                    await asyncio.sleep(0.01)
                assert len(scene.snapshot()["questionVisionPending"]) == 2
                duplicate.cancel()
                await asyncio.gather(duplicate, return_exceptions=True)
                release.set()
                first, second = await asyncio.gather(one, two)
            assert first["visual"]["description"] == "Evidence for color"
            assert second["visual"]["description"] == "Evidence for objects"
            assert first["visual"]["frame"] == scene.turns["question-one"]["frame"]
            assert len(calls) == 2
            assert scene.last_vision is None and not scene.vision_by_frame
            assert calls[0]["input"]["messages"][-1]["content"][0]["image"].startswith("data:image/jpeg;base64,")
            assert await scene.context("question-one", describe_frame=True, question="color") == first
        await scene.close()
    asyncio.run(run())


def test_visual_deadline_includes_queue_and_keeps_original_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("EGOASSIST_VISUAL_QUESTION_TIMEOUT", "0.1")

    async def run():
        scene = scene_with_frame(tmp_path)
        await scene.visual_questions.limit.acquire()
        await scene.visual_questions.limit.acquire()
        async with asyncio.timeout(1):
            context = await scene.context("question-one", describe_frame=True, question="color")
        assert context["visual"]["status"] == "unavailable"
        assert "TimeoutError" in context["visual"]["error"]
        assert context["visual"]["frame"] == scene.turns["question-one"]["frame"]
        assert "question-one" not in scene.visual_questions.results
        assert any(r["type"] == "scene.question.result" for r in scene.journal.entries())
        await scene.close()
    asyncio.run(run())


def test_end_to_end_latency_belongs_to_original_turn_and_updates_while_streaming(tmp_path):
    journal = SessionJournal(tmp_path / "case.jsonl", session_id="case")
    ledger = CaseWorkLedger(journal)
    start = datetime(2026, 9, 9, tzinfo=timezone.utc)
    for index in (0, 1):
        journal.append("turn_inbox", {"turnId": str(index), "text": str(index), "status": "completed",
            "timestamp": (start + timedelta(seconds=index * 10)).isoformat()})
        journal.append("answer.job", {"turnId": str(index), "status": "foreground"})
        journal.append("runtime_event", {"eventType": "response.created", "responseId": "r" + str(index),
            "model": "actual-model", "payload": {"origin": {"turnId": str(index)}}})
        journal.append("runtime_event", {"eventType": "response.first_audio", "responseId": "r" + str(index),
            "timestamp": (start + timedelta(seconds=index * 10 + 1 + index)).isoformat()})
    first, second = ledger.snapshot()["turnInbox"]
    assert first["answer"]["inputToFirstAudioMs"] == 1000
    assert second["answer"]["inputToFirstAudioMs"] == 2000
    assert first["answer"]["model"] == "actual-model"
    assert "inputToCompleteMs" not in first["answer"]


def test_scene_http_connection_reused_across_visual_lanes_and_closed(tmp_path, monkeypatch):
    import dashscope

    async def run():
        scene = scene_with_frame(tmp_path)
        transports = []

        async def answer(request):
            body = await request.json()
            transports.append(request.transport)
            text = '{"description":"red","people":[]}' if body["parameters"].get("response_format") else "red"
            return web.json_response({"output": {"choices": [{"message": {"content": [{"text": text}]}, "finish_reason": "stop"}]}})

        app = web.Application()
        app.router.add_post("/services/aigc/multimodal-generation/generation", answer)
        async with TestServer(app) as server:
            monkeypatch.setattr(dashscope, "base_http_api_url", str(server.make_url("")))
            for turn in ("question-one", "question-two"):
                result = await scene.context(turn, describe_frame=True, question="color")
                assert result["visual"]["status"] == "observed"
            assert (await scene.describe(scene.turns["question-one"]["frame"]))["status"] == "observed"
            assert len(transports) == 3 and len(set(transports)) == 1
            http = scene.http()
            await scene.close()
            assert http.closed
        await scene.close()
    asyncio.run(run())
