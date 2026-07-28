import numpy as np

from egoassist.backends.base import Answer, VLMBackend
from egoassist.config import Config
from egoassist.memory.store import MemoryItem
from egoassist.retrieval.localize import localize


class TimestampBackend(VLMBackend):
    dim = 2

    def embed_frames(self, frames):
        value = float(np.mean([frame[0, 0, 0] for frame in frames]))
        return np.array([value, 1.0 - value], dtype=np.float32)

    def embed_text(self, text):
        return np.array([1.0, 0.0], dtype=np.float32)

    def caption(self, frames):
        return ""

    def answer(self, query, evidence_texts, evidence_frames):
        return Answer("", 0.0)


def test_localization_uses_original_irregular_timestamps():
    frames = [
        np.full((1, 1, 3), value, dtype=np.float32)
        for value in (0.0, 1.0, 0.0)
    ]
    item = MemoryItem(
        id=0,
        t_start=0.0,
        t_end=10.0,
        embedding=np.array([1.0, 0.0], dtype=np.float32),
        caption="",
        keyframes=frames,
        keyframe_times=[0.0, 9.0, 10.0],
    )
    result = localize(
        "target", item, TimestampBackend(),
        Config(localize_win=2.0, localize_stride=1.0),
    )

    assert result.t0 == 6.0
    assert result.t0 <= 9.0 <= result.t1
    assert len(result.frames) == 1
    assert result.frame_times == [9.0]
