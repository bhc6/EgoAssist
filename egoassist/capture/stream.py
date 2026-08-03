"""采集层：把不同来源（合成 / 视频文件 / 摄像头 / 智能眼镜）统一成帧流。

上层只依赖 FrameStream.__iter__ 产出的 (timestamp, frame, meta)。
- 合成流：InMemoryStream（单元测试与离线评测可复现）
- 真实视频：VideoFileStream（需 opencv）
- 摄像头：WebcamStream（需 opencv）
- 智能眼镜：仿 VisionClaw 的手机 App 把逐帧+音频 POST 过来，接成同一接口即可
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Iterator, Optional

import numpy as np


@dataclass
class FrameEvent:
    t: float                            # 相对时间戳（秒），τ = t - t0
    frame: np.ndarray                   # HxWx3
    audio: Optional[str] = None         # 该帧对应的转写文本（若有）
    gaze: Optional[tuple] = None        # 注视点 (x,y) ∈ [0,1]^2，预留（G-C4）
    meta: dict = field(default_factory=dict)
    wall_time: Optional[datetime] = None  # 真实挂钟时间（带时区），None 表示未知


class FrameStream:
    """帧流基类。子类实现 __iter__。"""
    def __iter__(self) -> Iterator[FrameEvent]:
        raise NotImplementedError


class InMemoryStream(FrameStream):
    """从内存中的 FrameEvent 列表回放（合成数据 / 单元测试用）。"""
    def __init__(self, events: list[FrameEvent]):
        self._events = sorted(events, key=lambda e: e.t)

    def __iter__(self) -> Iterator[FrameEvent]:
        yield from self._events


class VideoFileStream(FrameStream):
    """读本地第一视角视频（Ego4D / 自录），按 fps 采样。需要 opencv-python。

    Args:
        recording_start: 视频第 0 帧对应的真实挂钟时间（带时区）。
            提供后每帧的 wall_time = recording_start + timedelta(seconds=t)，
            支持"我几点放的钥匙"这类绝对时刻查询。不提供则 wall_time=None。
    """
    def __init__(self, path: str, fps: float = 1.0, transcript: Optional[dict] = None,
                 recording_start: Optional[datetime] = None):
        self.path, self.fps, self.transcript = path, fps, (transcript or {})
        self.recording_start = recording_start

    def __iter__(self) -> Iterator[FrameEvent]:
        try:
            import cv2
        except ImportError as e:
            raise ImportError("pip install opencv-python 才能读视频文件") from e
        cap = cv2.VideoCapture(self.path)
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        step = max(1, int(round(src_fps / self.fps)))
        idx = 0
        last_t = -1.0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                nominal_t = idx / src_fps
                pos_msec = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
                decoder_t = pos_msec / 1000.0
                has_decoder_time = (pos_msec > 0.0 or idx == 0) and decoder_t > last_t
                t = decoder_t if has_decoder_time else nominal_t
                wall = (self.recording_start + timedelta(seconds=t)
                        if self.recording_start is not None else None)
                yield FrameEvent(
                    t=t,
                    frame=frame[:, :, ::-1],
                    audio=self.transcript.get(round(t)),
                    meta={
                        "source_frame_index": idx,
                        "timestamp_source": (
                            "decoder_pos_msec" if has_decoder_time else "frame_index_fps"
                        ),
                    },
                    wall_time=wall,
                )
                last_t = t
            idx += 1
        cap.release()


class WebcamStream(FrameStream):
    """笔记本/手机摄像头模拟第一视角实时流。需要 opencv-python。

    实时流始终记录真实挂钟时间（UTC），供"我刚才几点…"查询使用。
    """
    def __init__(self, cam: int = 0, fps: float = 1.0, max_seconds: float = 60.0):
        self.cam, self.fps, self.max_seconds = cam, fps, max_seconds

    def __iter__(self) -> Iterator[FrameEvent]:
        try:
            import cv2
            import time
        except ImportError as e:
            raise ImportError("pip install opencv-python 才能用摄像头") from e
        cap = cv2.VideoCapture(self.cam)
        t0 = time.time()
        while True:
            t = time.time() - t0
            if t > self.max_seconds:
                break
            ok, frame = cap.read()
            if not ok:
                break
            yield FrameEvent(
                t=t,
                frame=frame[:, :, ::-1],
                wall_time=datetime.now(timezone.utc),
            )
            time.sleep(1.0 / self.fps)
        cap.release()
