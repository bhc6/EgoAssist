from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .store import HierarchicalMemory, MemoryItem
from ..retrieval.index import build_index


FORMAT_VERSION = 2
SUPPORTED_VERSIONS = {1, FORMAT_VERSION}


def save_memory(
    memory: HierarchicalMemory,
    directory: str | Path,
    *,
    provenance: Mapping[str, Any] | None = None,
) -> Path:
    with memory._lock:
        return _save_memory(memory, directory, provenance=provenance)


def _save_memory(memory, directory, *, provenance=None) -> Path:

    root = Path(directory)
    frames_root = root / "frames"
    frames_root.mkdir(parents=True, exist_ok=True)
    by_id = dict(memory._items_by_id)
    items = sorted(by_id.values(), key=lambda item: item.id)
    embeddings = {}
    records = []
    for item in items:
        key = f"memory_{item.id}"
        embeddings[key] = np.asarray(item.embedding, dtype=np.float32)
        frame_records = []
        item_frames = frames_root / str(item.id)
        item_frames.mkdir(parents=True, exist_ok=True)
        if not (
            len(item.keyframes)
            == len(item.keyframe_times)
            == len(item.keyframe_meta)
        ):
            raise ValueError(
                f"Memory item {item.id} has mismatched keyframe fields"
            )
        for index, (frame, timestamp, metadata) in enumerate(
            zip(item.keyframes, item.keyframe_times, item.keyframe_meta)
        ):
            relative = Path("frames") / str(item.id) / f"{index:03d}.jpg"
            image = _to_image(frame)
            image.save(root / relative, format="JPEG", quality=90)
            frame_records.append({
                "path": relative.as_posix(),
                "source_time_sec": float(timestamp),
                "meta": metadata,
            })
        records.append({
            "id": item.id,
            "t_start": item.t_start,
            "t_end": item.t_end,
            "wall_start": item.wall_start.isoformat() if item.wall_start else None,
            "wall_end": item.wall_end.isoformat() if item.wall_end else None,
            "caption": item.caption,
            "tier": item.tier,
            "child_ids": list(item.child_ids),
            "audio_events": list(item.audio_events),
            "embedding_key": key,
            "keyframes": frame_records,
        })
    np.savez_compressed(root / "embeddings.npz", **embeddings)
    manifest = {
        "format": "egoassist-memory-snapshot",
        "version": FORMAT_VERSION,
        "embedding_dim": int(memory.backend.dim),
        "next_id": memory._next_id,
        "since_consolidate": memory._since_consolidate,
        "current_ids": [item.id for item in memory.current],
        "short_term_ids": [item.id for item in memory.short_term],
        "long_term_ids": [item.id for item in memory.long_term],
        "items": records,
    }
    if provenance is not None:
        manifest["provenance"] = dict(provenance)
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest_path


def load_memory(
    memory: HierarchicalMemory,
    directory: str | Path,
    *,
    expected_provenance: Mapping[str, Any] | None = None,
) -> int:
    from PIL import Image

    root = Path(directory)
    manifest_path = root / "manifest.json"
    embeddings_path = root / "embeddings.npz"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "egoassist-memory-snapshot":
        raise ValueError("Not an EgoAssist memory snapshot")
    if expected_provenance is not None and manifest.get("provenance") != dict(expected_provenance):
        raise ValueError("Memory snapshot provenance does not match the requested run")
    version = manifest.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f"Unsupported memory snapshot version: {manifest.get('version')}")
    if manifest.get("embedding_dim") != memory.backend.dim:
        raise ValueError(
            f"Embedding dimension mismatch: snapshot={manifest.get('embedding_dim')} "
            f"backend={memory.backend.dim}"
        )
    by_id = {}
    with np.load(embeddings_path, allow_pickle=False) as embeddings:
        for record in manifest["items"]:
            item_id = int(record["id"])
            if item_id in by_id:
                raise ValueError(f"Duplicate memory id: {item_id}")
            frames = []
            frame_times = []
            frame_meta = []
            keyframe_records = record["keyframes"]
            for index, frame_record in enumerate(keyframe_records):
                if version == 1:
                    relative = frame_record
                    count = len(keyframe_records)
                    timestamp = (
                        float(record["t_start"])
                        if count <= 1
                        else float(record["t_start"])
                        + index * (float(record["t_end"]) - float(record["t_start"]))
                        / (count - 1)
                    )
                    metadata = {}
                else:
                    relative = frame_record["path"]
                    timestamp = float(frame_record["source_time_sec"])
                    metadata = dict(frame_record.get("meta", {}))
                path = _safe_snapshot_path(root, relative)
                with Image.open(path) as image:
                    frames.append(np.asarray(image.convert("RGB"), dtype=np.uint8))
                frame_times.append(timestamp)
                frame_meta.append(metadata)
            embedding = np.asarray(
                embeddings[record["embedding_key"]], dtype=np.float32
            )
            if embedding.shape != (memory.backend.dim,):
                raise ValueError(
                    f"Invalid embedding shape for memory {item_id}: {embedding.shape}"
                )
            t_start = float(record["t_start"])
            t_end = float(record["t_end"])
            if t_end < t_start:
                raise ValueError(f"Invalid time range for memory {item_id}")
            if any(
                timestamp < t_start - 1e-6 or timestamp > t_end + 1e-6
                for timestamp in frame_times
            ):
                raise ValueError(f"Keyframe timestamp outside memory {item_id} range")
            if frame_times != sorted(frame_times):
                raise ValueError(f"Keyframe timestamps are not ordered for memory {item_id}")
            by_id[item_id] = MemoryItem(
                id=item_id,
                t_start=t_start,
                t_end=t_end,
                wall_start=datetime.fromisoformat(record["wall_start"]) if record.get("wall_start") else None,
                wall_end=datetime.fromisoformat(record["wall_end"]) if record.get("wall_end") else None,
                embedding=embedding,
                caption=str(record["caption"]),
                keyframes=frames,
                keyframe_times=frame_times,
                keyframe_meta=frame_meta,
                audio_events=list(record.get("audio_events", [])),
                tier=str(record.get("tier", record.get("level", "short"))),
                child_ids=[int(value) for value in record["child_ids"]],
            )
    current = _resolve_ids(by_id, manifest["current_ids"], "current")
    short_term = _resolve_ids(by_id, manifest["short_term_ids"], "short_term")
    long_term = _resolve_ids(by_id, manifest["long_term_ids"], "long_term")
    if version == FORMAT_VERSION:
        for item in long_term:
            _resolve_ids(by_id, item.child_ids, f"children of memory {item.id}")
    next_id = int(manifest["next_id"])
    if by_id and next_id <= max(by_id):
        raise ValueError("next_id must be greater than every memory id")
    since_consolidate = int(manifest["since_consolidate"])
    index = build_index(memory.backend.dim)
    for item in short_term + long_term:
        index.add(item.id, item.embedding)
    with memory._lock:
        memory.current, memory.short_term, memory.long_term = current, short_term, long_term
        memory._items_by_id = by_id
        memory._next_id = next_id
        memory._since_consolidate = since_consolidate
        memory._index = index
    return len(by_id)


def _resolve_ids(by_id: dict[int, MemoryItem], ids: list[int], name: str):
    if len(ids) != len(set(map(int, ids))):
        raise ValueError(f"Duplicate memory id in {name}")
    try:
        return [by_id[int(item_id)] for item_id in ids]
    except KeyError as error:
        raise ValueError(f"Unknown memory id in {name}: {error.args[0]}") from error


def _safe_snapshot_path(root: Path, relative: str) -> Path:
    base = root.resolve()
    path = (base / relative).resolve()
    try:
        path.relative_to(base)
    except ValueError as error:
        raise ValueError(f"Snapshot path escapes root: {relative}") from error
    return path


def _to_image(frame: np.ndarray):
    from PIL import Image

    array = np.asarray(frame)
    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)
    if array.ndim == 2:
        array = np.stack([array] * 3, axis=-1)
    return Image.fromarray(array[:, :, :3])
