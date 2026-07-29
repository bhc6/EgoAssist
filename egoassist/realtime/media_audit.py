from __future__ import annotations

import time
from typing import Any, Callable


class MediaAuditAccumulator:
    """Aggregate transport telemetry before it reaches the durable case journal."""

    def __init__(
        self,
        trace: Callable[..., Any],
        *,
        flush_interval: float = 1.0,
        max_events: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.trace = trace
        self.flush_interval = max(0.1, float(flush_interval))
        self.max_events = max(1, int(max_events))
        self.clock = clock
        self._last_flush = clock()
        self._audio = {"chunks": 0, "base64Bytes": 0, "videoFlushes": 0}
        self._video = {"frames": 0, "base64Bytes": 0}

    def record_audio(self, base64_bytes: int, *, video_flushed: bool) -> None:
        self._audio["chunks"] += 1
        self._audio["base64Bytes"] += max(0, int(base64_bytes))
        self._audio["videoFlushes"] += int(bool(video_flushed))
        self._flush_if_due()

    def record_video(self, base64_bytes: int) -> None:
        self._video["frames"] += 1
        self._video["base64Bytes"] += max(0, int(base64_bytes))
        self._flush_if_due()

    def _flush_if_due(self) -> None:
        total = self._audio["chunks"] + self._video["frames"]
        if total >= self.max_events or self.clock() - self._last_flush >= self.flush_interval:
            self.flush()

    def flush(self) -> int:
        written = 0
        if self._audio["chunks"]:
            self.trace("media.audio.batch", "browser-microphone", dict(self._audio))
            written += 1
        if self._video["frames"]:
            self.trace("media.video.batch", "browser-camera", {
                **self._video,
                "policy": "latest-only",
            })
            written += 1
        self._audio = {"chunks": 0, "base64Bytes": 0, "videoFlushes": 0}
        self._video = {"frames": 0, "base64Bytes": 0}
        self._last_flush = self.clock()
        return written
