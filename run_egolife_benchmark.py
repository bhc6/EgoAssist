#!/usr/bin/env python3
"""Run the downloaded EgoLife subset with real local VLM inference."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import time
from pathlib import Path

import numpy as np

from egoassist.abstention import ConformalAbstention
from egoassist.backends import build_backend
from egoassist.capture.stream import VideoFileStream
from egoassist.config import Config
from egoassist.eval.real_metrics import exact_match, temporal_iou, token_f1
from egoassist.memory import TimelineMemory, import_visual_items, load_memory, save_memory
from egoassist.qa import StreamingMemoryAssistant, TimelineAssistant


RUN_MANIFEST_VERSION = 1


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


def fingerprint_payload(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_run_manifest(path: Path, manifest: dict, *, artifacts_exist: bool) -> str:
    expected = dict(manifest)
    fingerprint = fingerprint_payload(expected)
    expected["fingerprint"] = fingerprint
    if path.exists():
        existing = read_json(path)
        if existing != expected:
            raise RuntimeError(
                f"Benchmark output provenance mismatch at {path}. "
                "Use a new --output-dir or remove the stale output artifacts."
            )
    elif artifacts_exist:
        raise RuntimeError(
            f"Benchmark output contains results or memory without provenance: {path.parent}. "
            "Use a new --output-dir or remove the stale output artifacts."
        )
    else:
        path.write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")
    return fingerprint


def validate_existing_results(
    rows: list[dict],
    expected_keys: set[str],
    video_hashes: dict[str, str],
) -> set[str]:
    keys = [str(row.get("key", "")) for row in rows]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for key in keys:
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    if duplicates:
        raise RuntimeError(f"Duplicate benchmark result key(s): {', '.join(sorted(duplicates)[:5])}")
    unexpected = sorted(set(keys) - expected_keys)
    if unexpected:
        raise RuntimeError(f"Out-of-scope benchmark result key(s): {', '.join(unexpected[:5])}")
    for row in rows:
        video_id = str(row.get("video_id", ""))
        if video_id not in video_hashes or row.get("video_sha256") != video_hashes[video_id]:
            raise RuntimeError(f"Benchmark result source provenance mismatch for key: {row.get('key')}")
    return set(keys)


def clean_question(value: str) -> str:
    return value.replace("<speech>\n<image>\n", "").strip()


def video_metadata(path: Path) -> tuple[float, int, float]:
    import cv2

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
    """Split videos into calibration and test sets.

    With fewer than two videos there is no honest way to hold out a test set, so
    the test set is returned empty rather than silently reusing calibration rows.
    Callers must report unmeasured metrics as null, never as zero.
    """
    shuffled = list(video_ids)
    random.Random(seed).shuffle(shuffled)
    if len(shuffled) < 2:
        return set(shuffled), set()
    count = max(1, min(len(shuffled) - 1, round(len(shuffled) * calibration_ratio)))
    return set(shuffled[:count]), set(shuffled[count:])


def mean_or_none(rows: list[dict], key: str) -> float | None:
    """Mean over rows, or None when there is nothing to average.

    Returning None keeps "not measured" distinguishable from "measured as zero".
    """
    if not rows:
        return None
    return float(np.mean([row[key] for row in rows]))


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
    if not video_ids:
        split_status = "no-rows"
    elif not test:
        split_status = "no-test-split"
    else:
        split_status = "held-out-test-split"
    return {
        "scope": "downloaded_egolife_subset",
        "n_videos": len(video_ids),
        "n_questions": len(rows),
        "calibration_videos": sorted(calibration_videos),
        "test_videos": sorted(test_videos),
        "n_calibration": len(calibration),
        "n_test": len(test),
        "split_status": split_status,
        "alpha": alpha,
        "threshold": conformal.threshold if math.isfinite(conformal.threshold) else None,
        # Held-out test metrics. null when no test split exists -- a single video
        # cannot be both calibrated and tested on, and reporting 0.0 there would
        # claim a measurement that was never made.
        "coverage": len(answered) / len(test) if test else None,
        "selective_risk": 1 - float(np.mean(answered_correct)) if answered else None,
        "all_answer_risk": 1 - float(np.mean(test_correct)) if test else None,
        "exact_match": mean_or_none(test, "exact_match"),
        "token_f1": mean_or_none(test, "token_f1"),
        "localization_miou": mean_or_none(test, "temporal_iou"),
        "localization_acc_05": (
            float(np.mean([row["temporal_iou"] >= 0.5 for row in test])) if test else None
        ),
        "invalid_generation_rate": (
            float(np.mean([
                row.get("invalid_generation", row.get("invalid_answer", False)) for row in test
            ])) if test else None
        ),
        "raw_abstention_rate": (
            float(np.mean([row.get("abstained", False) for row in test])) if test else None
        ),
        # All-sample metrics. Not a held-out measurement -- reported so a
        # single-video smoke run still surfaces real per-question numbers
        # instead of looking like a total failure.
        "all_sample_exact_match": mean_or_none(rows, "exact_match"),
        "all_sample_token_f1": mean_or_none(rows, "token_f1"),
        "all_sample_localization_miou": mean_or_none(rows, "temporal_iou"),
        "invalid_caption_rate": mean_or_none(rows, "invalid_caption"),
        "mean_query_seconds": mean_or_none(rows, "query_seconds"),
        "n_queries_with_evidence": sum(bool(row.get("candidate_memory_ids")) for row in rows),
        "n_queries_without_evidence": sum(not row.get("candidate_memory_ids") for row in rows),
        "mean_query_with_evidence_seconds": mean_or_none(
            [row for row in rows if row.get("candidate_memory_ids")], "query_seconds"
        ),
        "mean_query_without_evidence_seconds": mean_or_none(
            [row for row in rows if not row.get("candidate_memory_ids")], "query_seconds"
        ),
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
    parser.add_argument(
        "--memory-backend",
        choices=("hierarchical", "timeline"),
        default="timeline",
        help="Memory system used during ingest and retrieval (default: timeline).",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = args.output_dir / "progress.jsonl"
    results_path = args.output_dir / "results.jsonl"
    summary_path = args.output_dir / "summary.json"
    run_manifest_path = args.output_dir / "run-manifest.json"
    timeline_path = args.output_dir / "timeline.sqlite3"
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

    video_hashes = {video.stem: sha256(video) for video in videos}
    run_manifest = {
        "schemaVersion": RUN_MANIFEST_VERSION,
        "backend": args.backend,
        "model": args.model_id if args.backend == "local" else args.api_model,
        "precision": args.precision,
        "encoderDevice": args.encoder_device,
        "sampleFps": args.fps,
        "sampling": {
            "seed": args.seed,
            "maxVideos": args.max_videos,
            "maxQuestionsPerVideo": args.max_questions_per_video,
            "alpha": args.alpha,
            "calibrationRatio": args.calibration_ratio,
        },
        "memoryBackend": args.memory_backend,
        "qaSha256": sha256(qa_path),
        "videos": [
            {"id": video.stem, "name": video.name, "sha256": video_hashes[video.stem]}
            for video in videos
        ],
    }
    artifacts_exist = (
        results_path.exists()
        or summary_path.exists()
        or timeline_path.exists()
        or any((args.output_dir / "memory").glob("*/manifest.json"))
    )
    run_fingerprint = ensure_run_manifest(
        run_manifest_path, run_manifest, artifacts_exist=artifacts_exist,
    )
    expected_keys = {
        row["key"] for rows_for_video in grouped.values() for row in rows_for_video
    }
    existing_results = read_jsonl(results_path)
    completed = validate_existing_results(existing_results, expected_keys, video_hashes)
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
    cfg = Config()
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
        snapshot_dir = args.output_dir / "memory" / video.stem
        snapshot_provenance = {
            "runFingerprint": run_fingerprint,
            "videoId": video.stem,
            "videoSha256": video_hashes[video.stem],
            "sampleFps": args.fps,
        }

        if args.memory_backend == "timeline":
            # ── Timeline path (new memory system) ──────────────────────────────
            # Captions are stored directly into the shared timeline SQLite;
            # no raw frames are retained after ingest.
            tl = TimelineMemory(timeline_path)
            assistant = TimelineAssistant(
                backend, cfg,
                timeline=tl,
                user_id="egolife-benchmark",
                case_id=video.stem,
            )
            manifest_path = snapshot_dir / "manifest.json"
            cached = assistant.load_if_cached(manifest_path, snapshot_provenance)
            if cached is not None:
                segment_count = cached
                memory_reused = True
            else:
                segment_count = assistant.ingest(VideoFileStream(str(video), fps=args.fps))
                assistant.save_manifest(manifest_path, snapshot_provenance)
                memory_reused = False
            ingest_seconds = time.perf_counter() - ingest_started
            captions = assistant.all_captions()
            caption = " | ".join(captions)
            invalid_caption = (
                "[no visual content]" in caption or "[invalid visual caption]" in caption
            )
            append_jsonl(progress_path, {
                "event": "memory_saved", "time": time.time(), "video": video.name,
                "manifest": str(manifest_path), "items": segment_count,
                "memory_backend": "timeline", "reused": memory_reused,
            })

        else:
            # ── Hierarchical path (original memory system) ─────────────────────
            assistant = StreamingMemoryAssistant(backend, cfg)
            if (snapshot_dir / "manifest.json").exists():
                segment_count = load_memory(
                    assistant.memory,
                    snapshot_dir,
                    expected_provenance=snapshot_provenance,
                )
                memory_reused = True
            else:
                segment_count = assistant.ingest(VideoFileStream(str(video), fps=args.fps))
                memory_reused = False
            ingest_seconds = time.perf_counter() - ingest_started
            memories = assistant.memory.all_items()
            timeline_cards = import_visual_items(
                TimelineMemory(timeline_path),
                memories,
                user_id="egolife-benchmark",
                case_id=video.stem,
                source=f"egolife:{video.stem}:{video_hashes[video.stem]}",
            )
            caption = " | ".join(item.caption for item in memories)
            invalid_caption = "[invalid visual caption]" in caption
            snapshot_path = save_memory(
                assistant.memory, snapshot_dir, provenance=snapshot_provenance,
            )
            append_jsonl(progress_path, {
                "event": "memory_saved", "time": time.time(), "video": video.name,
                "manifest": str(snapshot_path), "items": len(memories),
                "memory_backend": "hierarchical",
                "timeline": str(timeline_path), "timeline_cards": len(timeline_cards),
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
                "i am not sure", "i'm not sure", "evidence is insufficient",
                "i don't have any relevant memory.",
            }
            output = {
                "key": row["key"], "video_id": video.stem, "video": video.name,
                "video_sha256": video_hashes[video.stem], "source_fps": fps,
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

    all_results = [
        row for row in read_jsonl(results_path) if row.get("key") in expected_keys
    ]
    summary = build_summary(all_results, args.alpha, args.seed, args.calibration_ratio)
    summary.update({
        "model": evaluated_model,
        "backend_class": type(backend).__name__,
        "encoder_backend": getattr(getattr(backend, "_encoder", None), "_backend", "not-used"),
        "sample_fps": args.fps,
        "precision": args.precision,
        "encoder_device": args.encoder_device,
        "api_usage": getattr(backend, "usage", None),
        "results_path": str(results_path),
        "run_fingerprint": run_fingerprint,
    })
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    append_jsonl(progress_path, {"event": "complete", "time": time.time(), **summary})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
