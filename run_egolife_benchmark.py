#!/usr/bin/env python3
"""Run the downloaded EgoLife subset with real local VLM inference."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import time
from pathlib import Path

import cv2
import numpy as np

from egoassist.abstention import ConformalAbstention
from egoassist.backends import build_backend
from egoassist.capture.stream import VideoFileStream
from egoassist.config import Config
from egoassist.eval.real_metrics import exact_match, temporal_iou, token_f1
from egoassist.memory import load_memory, save_memory
from egoassist.qa import StreamingMemoryAssistant


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def append_jsonl(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_question(value: str) -> str:
    return value.replace("<speech>\n<image>\n", "").strip()


def video_metadata(path: Path) -> tuple[float, int, float]:
    capture = cv2.VideoCapture(str(path))
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    capture.release()
    return fps, frames, frames / fps if fps else 0.0


def sample_rows(rows: list[dict], max_questions: int | None, seed: int) -> list[dict]:
    if max_questions is None or len(rows) <= max_questions:
        return rows
    rng = random.Random(seed)
    return sorted(rng.sample(rows, max_questions), key=lambda row: row["key"])


def split_videos(video_ids: list[str], calibration_ratio: float, seed: int):
    shuffled = list(video_ids)
    random.Random(seed).shuffle(shuffled)
    count = max(1, min(len(shuffled) - 1, round(len(shuffled) * calibration_ratio)))
    return set(shuffled[:count]), set(shuffled[count:])


def build_summary(rows: list[dict], alpha: float, seed: int, calibration_ratio: float):
    video_ids = sorted({row["video_id"] for row in rows})
    calibration_videos, test_videos = split_videos(video_ids, calibration_ratio, seed)
    calibration = [row for row in rows if row["video_id"] in calibration_videos]
    test = [row for row in rows if row["video_id"] in test_videos]
    correct = [row["token_f1"] >= 0.5 for row in calibration]
    conformal = ConformalAbstention(alpha=alpha).fit(
        [row["score"] for row in calibration], correct
    )
    answered = [row for row in test if row["score"] >= conformal.threshold]
    test_correct = [row["token_f1"] >= 0.5 for row in test]
    answered_correct = [row["token_f1"] >= 0.5 for row in answered]
    curve = conformal.risk_coverage_curve(
        [row["score"] for row in test], test_correct
    )
    return {
        "scope": "downloaded_egolife_subset",
        "n_videos": len(video_ids),
        "n_questions": len(rows),
        "calibration_videos": sorted(calibration_videos),
        "test_videos": sorted(test_videos),
        "n_calibration": len(calibration),
        "n_test": len(test),
        "alpha": alpha,
        "threshold": conformal.threshold if math.isfinite(conformal.threshold) else None,
        "coverage": len(answered) / len(test) if test else 0.0,
        "selective_risk": 1 - float(np.mean(answered_correct)) if answered else 0.0,
        "all_answer_risk": 1 - float(np.mean(test_correct)) if test else 0.0,
        "exact_match": float(np.mean([row["exact_match"] for row in test])) if test else 0.0,
        "token_f1": float(np.mean([row["token_f1"] for row in test])) if test else 0.0,
        "localization_miou": float(np.mean([row["temporal_iou"] for row in test])) if test else 0.0,
        "localization_acc_05": float(np.mean([row["temporal_iou"] >= 0.5 for row in test])) if test else 0.0,
        "invalid_generation_rate": float(np.mean([
            row.get("invalid_generation", row.get("invalid_answer", False)) for row in test
        ])) if test else 0.0,
        "raw_abstention_rate": float(np.mean([
            row.get("abstained", False) for row in test
        ])) if test else 0.0,
        "invalid_caption_rate": float(np.mean([row["invalid_caption"] for row in rows])) if rows else 0.0,
        "mean_query_seconds": float(np.mean([row["query_seconds"] for row in rows])) if rows else 0.0,
        "curve": [
            {"threshold": threshold, "coverage": coverage, "risk": risk}
            for threshold, coverage, risk in curve
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/egolife_benchmark"))
    parser.add_argument("--fps", type=float, default=0.5)
    parser.add_argument("--max-videos", type=int)
    parser.add_argument("--max-questions-per-video", type=int)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--alpha", type=float, default=0.1)
    parser.add_argument("--calibration-ratio", type=float, default=0.5)
    parser.add_argument("--precision", choices=("fp16", "4bit"), default="fp16")
    parser.add_argument("--encoder-device", default="cpu")
    parser.add_argument("--model-id", default="Qwen/Qwen3-VL-2B-Instruct")
    parser.add_argument("--backend", choices=("local", "dashscope"), default="local")
    parser.add_argument("--api-model", default="qwen3-vl-plus")
    parser.add_argument("--video-id", action="append", default=[])
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = args.output_dir / "progress.jsonl"
    results_path = args.output_dir / "results.jsonl"
    summary_path = args.output_dir / "summary.json"
    qa_path = args.data_dir / "EgoLife_QA.json"
    videos = sorted(args.data_dir.glob("*.mp4"))
    if args.video_id:
        selected = set(args.video_id)
        videos = [video for video in videos if video.stem in selected]
        missing = selected - {video.stem for video in videos}
        if missing:
            parser.error(f"Unknown video id(s): {', '.join(sorted(missing))}")
    if args.max_videos:
        videos = videos[: args.max_videos]
    video_ids = {video.stem for video in videos}
    qa_rows = [row for row in read_json(qa_path) if row["id"] in video_ids]
    grouped = {video.stem: [] for video in videos}
    for index, row in enumerate(qa_rows):
        question = clean_question(row["conversations"][0]["value"])
        row = dict(row)
        row["question"] = question
        row["answer"] = row["conversations"][1]["value"].strip()
        row["key"] = f"{row['id']}:{row['start_frame']}:{row['end_frame']}:{index}"
        grouped[row["id"]].append(row)
    for video_id in grouped:
        grouped[video_id] = sample_rows(
            grouped[video_id], args.max_questions_per_video, args.seed
        )

    completed = {row["key"] for row in read_jsonl(results_path)}
    total = sum(len(rows) for rows in grouped.values())
    append_jsonl(progress_path, {
        "event": "start", "time": time.time(), "total": total,
        "completed": len(completed), "videos": len(videos),
        "backend": args.backend,
        "model": args.model_id if args.backend == "local" else args.api_model,
    })

    if args.backend == "local":
        backend = build_backend(
            "local", device="cuda", load_in_4bit=args.precision == "4bit",
            encoder_device=args.encoder_device, model_id=args.model_id,
        )
        evaluated_model = args.model_id
    else:
        backend = build_backend(
            "dashscope", device=args.encoder_device, model=args.api_model,
        )
        evaluated_model = args.api_model
    cfg = Config(router_recent_bias=0.0)
    for video_index, video in enumerate(videos, start=1):
        pending = [row for row in grouped[video.stem] if row["key"] not in completed]
        if not pending:
            continue
        fps, frame_count, duration = video_metadata(video)
        append_jsonl(progress_path, {
            "event": "video_start", "time": time.time(), "video": video.name,
            "video_index": video_index, "video_count": len(videos),
            "pending_questions": len(pending),
        })
        ingest_started = time.perf_counter()
        assistant = StreamingMemoryAssistant(backend, cfg)
        snapshot_dir = args.output_dir / "memory" / video.stem
        if (snapshot_dir / "manifest.json").exists():
            segment_count = load_memory(assistant.memory, snapshot_dir)
            memory_reused = True
        else:
            segment_count = assistant.ingest(VideoFileStream(str(video), fps=args.fps))
            memory_reused = False
        ingest_seconds = time.perf_counter() - ingest_started
        memories = assistant.memory.all_items()
        caption = " | ".join(item.caption for item in memories)
        invalid_caption = "[invalid visual caption]" in caption
        snapshot_path = save_memory(
            assistant.memory, snapshot_dir
        )
        append_jsonl(progress_path, {
            "event": "memory_saved", "time": time.time(), "video": video.name,
            "manifest": str(snapshot_path), "items": len(memories),
            "reused": memory_reused,
        })
        for row in pending:
            query_started = time.perf_counter()
            result = assistant.query(row["question"])
            query_seconds = time.perf_counter() - query_started
            gt_start = row["start_frame"] / fps
            gt_end = row["end_frame"] / fps
            invalid_generation = "response was invalid" in result.answered_text
            abstained = result.answered_text.strip().lower() in {
                "i am not sure", "i'm not sure", "evidence is insufficient"
            }
            output = {
                "key": row["key"], "video_id": video.stem, "video": video.name,
                "video_sha256": sha256(video), "source_fps": fps,
                "source_frames": frame_count, "source_seconds": duration,
                "question": row["question"], "reference": row["answer"],
                "prediction": result.answered_text,
                "gt_start": gt_start, "gt_end": gt_end,
                "pred_start": result.t0, "pred_end": result.t1,
                "top_memory_id": result.top_memory_id,
                "candidate_memory_ids": result.candidate_ids,
                "evidence_frame_times": result.evidence_frame_times,
                "score": result.score, "vlm_conf": result.vlm_conf,
                "retrieval_sim": result.retrieval_sim,
                "localize_conf": result.localize_conf,
                "top_caption": result.top_caption,
                "exact_match": exact_match(result.answered_text, row["answer"]),
                "token_f1": token_f1(result.answered_text, row["answer"]),
                "temporal_iou": temporal_iou(result.t0, result.t1, gt_start, gt_end),
                "invalid_generation": invalid_generation,
                "abstained": abstained,
                "invalid_caption": invalid_caption,
                "segment_count": segment_count, "ingest_seconds": ingest_seconds,
                "memory_reused": memory_reused,
                "query_seconds": query_seconds,
            }
            append_jsonl(results_path, output)
            completed.add(row["key"])
            append_jsonl(progress_path, {
                "event": "question_done", "time": time.time(),
                "completed": len(completed), "total": total,
                "video": video.name, "question": row["question"],
                "prediction": result.answered_text, "reference": row["answer"],
                "token_f1": output["token_f1"], "temporal_iou": output["temporal_iou"],
                "score": result.score, "query_seconds": query_seconds,
            })
            print(f"[{len(completed)}/{total}] {video.name} F1={output['token_f1']:.3f} "
                  f"tIoU={output['temporal_iou']:.3f} {query_seconds:.1f}s")
        append_jsonl(progress_path, {
            "event": "video_done", "time": time.time(), "video": video.name,
            "ingest_seconds": ingest_seconds, "segments": segment_count,
            "invalid_caption": invalid_caption,
        })

    all_results = read_jsonl(results_path)
    summary = build_summary(all_results, args.alpha, args.seed, args.calibration_ratio)
    summary.update({
        "model": evaluated_model,
        "backend_class": type(backend).__name__,
        "encoder_backend": getattr(backend.encoder, "_backend", "unknown"),
        "sample_fps": args.fps,
        "precision": args.precision,
        "encoder_device": args.encoder_device,
        "api_usage": getattr(backend, "usage", None),
        "results_path": str(results_path),
    })
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    append_jsonl(progress_path, {"event": "complete", "time": time.time(), **summary})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
