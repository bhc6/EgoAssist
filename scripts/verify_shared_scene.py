"""Exercise real Qwen vision and local acoustic models on an existing EgoLife clip."""
import argparse
import asyncio
import base64
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import av
from egoassist._env import load_env
from egoassist.realtime.context_harness import SessionJournal
from egoassist.realtime.scene import SharedScene
from egoassist.product.answer_jobs import AnswerJobs
import os


def decode(source, seconds=12):
    with av.open(str(source)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        chunks = []
        for frame in container.decode(audio=0):
            if frame.time and frame.time > seconds:
                break
            for converted in resampler.resample(frame):
                chunks.append(converted.to_ndarray().tobytes())
        pcm = b"".join(chunks)[:seconds * 32000]
    with av.open(str(source)) as container:
        for frame in container.decode(video=0):
            if frame.time is not None and frame.time >= min(3, seconds):
                out = io.BytesIO()
                frame.to_image().resize((768, 432)).save(out, format="JPEG")
                return pcm, out.getvalue()
    raise ValueError("No video frame")


async def verify(source, output):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "mock": False, "source": str(source), "hardwareMeasured": False}
    scene = SharedScene(SessionJournal(output / "scene.jsonl", session_id="scene"),
                        os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY"))
    try:
        pcm, image = decode(source)
        (output / "input.jpg").write_bytes(image)
        scene.append_frame(base64.b64encode(image).decode())
        for offset in range(0, len(pcm), 3200):
            scene.append_audio(base64.b64encode(pcm[offset:offset + 3200]).decode(), 1)
        scene.capture("speech", segment={"audioStartMs": 0, "audioEndMs": len(pcm) / 32, "generation": 1})
        speaker = await scene.resolve_speaker("speech")
        report["speaker"] = speaker["speaker"]
        assert report["speaker"]["segments"], report["speaker"]
        assert not report["speaker"]["wearer"]
        report["context"] = await scene.context("speech")
        assert report["context"]["visual"]["frame"], report["context"]
        report["vision"] = await scene.describe(report["context"]["visual"]["frame"])
        assert report["vision"]["status"] == "observed", report["vision"]
        manager = AnswerJobs(scene.api_key)
        result = await manager._generate(scene.journal, {"turnId": "speech", "question": "描述图片中实际可见的主要物品与场景，不要猜测姓名。",
            "instructions": "Answer based on the supplied actual frame.", "context": {"sharedScene": report["context"]}})
        report["answer"] = result
        assert result["providerRequestId"] and result["frameEvidence"]["sha256"] == report["context"]["visual"]["frame"]["sha256"]
        from verify_real_voice_stop import synthesize
        voices = []
        for index, (voice, text) in enumerate([
            ("Cherry", "今天我们在会议室讨论了智能眼镜的测试计划。我负责检查摄像头，明天下午三点再来确认结果。"),
            ("Ethan", "我负责检查麦克风和扬声器，测试记录放在桌面上的文件夹里。请你先完成画面的检查，我随后开始录音。"),
        ]):
            audio, evidence = await synthesize(text, output, index, voice)
            voices.append(audio)
            report.setdefault("syntheticSources", []).append({"voice": voice, **evidence})
        combined = voices[0] + bytes(16000) + voices[1]
        scene.append_audio(base64.b64encode(combined[:320000]).decode(), 2)
        for offset in range(320000, len(combined), 320000):
            scene.append_audio(base64.b64encode(combined[offset:offset + 320000]).decode(), 2)
        scene.capture("two-voices", segment={"audioStartMs": 0, "audioEndMs": len(combined) / 32, "generation": 2})
        mixed = await scene.resolve_speaker("two-voices")
        report["twoVoices"] = mixed["speaker"]
        assert len({s["cluster"] for s in mixed["speaker"]["segments"]}) == 2, mixed["speaker"]
        assert not mixed["speaker"]["wearer"]
        pending = list(scene.tasks)
        await asyncio.gather(*pending)
        report["segmentedTranscript"] = scene.turns["two-voices"].get("utterances", [])
        assert report["segmentedTranscript"] and all(s["status"] == "transcribed" and s["requestId"] for s in report["segmentedTranscript"])
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        await scene.close()
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(verify(args.source, args.output)) else 1)
