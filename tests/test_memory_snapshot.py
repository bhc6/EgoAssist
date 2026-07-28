import json

import numpy as np
import pytest

from egoassist.backends import MockBackend
from egoassist.capture.stream import InMemoryStream
from egoassist.config import Config
from egoassist.data import make_day
from egoassist.memory import HierarchicalMemory, load_memory, save_memory
from egoassist.qa import StreamingMemoryAssistant


def test_memory_snapshot_round_trip(tmp_path):
    cfg = Config(seed=4)
    backend = MockBackend(seed=4)
    stream, _ = make_day(n_events=12, seed=4)
    source = StreamingMemoryAssistant(backend, cfg)
    source.ingest(InMemoryStream(stream))

    manifest_path = save_memory(source.memory, tmp_path / "snapshot")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["format"] == "egoassist-memory-snapshot"
    assert (tmp_path / "snapshot" / "embeddings.npz").exists()

    restored = StreamingMemoryAssistant(MockBackend(seed=4), cfg)
    count = load_memory(restored.memory, tmp_path / "snapshot")
    assert count >= len(source.memory.all_items())
    assert [item.id for item in restored.memory.short_term] == [
        item.id for item in source.memory.short_term
    ]
    assert [item.id for item in restored.memory.long_term] == [
        item.id for item in source.memory.long_term
    ]
    for original, loaded in zip(
        sorted(source.memory.all_items(), key=lambda item: item.id),
        sorted(restored.memory.all_items(), key=lambda item: item.id),
    ):
        assert loaded.caption == original.caption
        assert loaded.t_start == original.t_start
        assert loaded.t_end == original.t_end
        assert loaded.child_ids == original.child_ids
        np.testing.assert_allclose(loaded.embedding, original.embedding)
        assert len(loaded.keyframes) == len(original.keyframes)
        assert loaded.keyframe_times == original.keyframe_times
        assert loaded.keyframe_meta == original.keyframe_meta
        assert loaded.audio_events == original.audio_events
        assert loaded.keyframes[0].shape == original.keyframes[0].shape

    result = restored.query("where are my keys?")
    assert result.n_candidates > 0
    assert result.answered_text

    registry_ids = set(restored.memory._items_by_id)
    assert all(
        child_id in registry_ids
        for item in restored.memory.long_term
        for child_id in item.child_ids
    )


def test_memory_snapshot_rejects_embedding_mismatch(tmp_path):
    source = HierarchicalMemory(MockBackend(seed=0), Config())
    save_memory(source, tmp_path / "snapshot")
    manifest_path = tmp_path / "snapshot" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["embedding_dim"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="Embedding dimension mismatch"):
        load_memory(source, tmp_path / "snapshot")


def test_memory_snapshot_loads_v1_with_interpolated_times(tmp_path):
    cfg = Config(seed=2)
    source = StreamingMemoryAssistant(MockBackend(seed=2), cfg)
    stream, _ = make_day(n_events=2, seed=2)
    source.ingest(InMemoryStream(stream))
    root = tmp_path / "snapshot"
    manifest_path = save_memory(source.memory, root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 1
    for record in manifest["items"]:
        record["keyframes"] = [frame["path"] for frame in record["keyframes"]]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    restored = StreamingMemoryAssistant(MockBackend(seed=2), cfg)
    load_memory(restored.memory, root)
    for item in restored.memory.all_items():
        assert len(item.keyframe_times) == len(item.keyframes)
        assert item.keyframe_times[0] == item.t_start
        if len(item.keyframes) > 1:
            assert item.keyframe_times[-1] == item.t_end
