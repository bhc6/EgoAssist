"""全局配置。所有可调超参集中在此，便于消融实验。"""

from dataclasses import dataclass


@dataclass
class Config:
    # ---- 事件分段 ----
    event_change_threshold: float = 0.35   # 跨帧变化超过此值则切事件边界
    event_min_len: float = 1.0             # 最短事件时长（秒），防止碎片化
    event_max_len: float = 30.0            # 强制切分的最长事件时长（秒）
    use_gaze_imu: bool = False             # 预留：注视/IMU 触发分段（G-C4）

    # ---- 分层记忆 ----
    current_window: float = 8.0            # current memory 保留最近多少秒
    short_term_capacity: int = 40          # short-term 事件条数上限，超出触发整理
    consolidate_every: int = 24            # 每摄入多少事件做一次异步整理

    # ---- 检索 ----
    retrieve_topk: int = 5                 # 候选事件数

    # ---- 精确时间定位 ----
    localize_stride: float = 0.5           # 候选窗内滑窗步长（秒）
    localize_win: float = 2.0              # 证据窗口长度（秒）

    # ---- 校准拒答（split conformal / selective prediction）----
    risk_alpha: float = 0.1                # 目标风险上界（允许的最大错误率）
    calib_ratio: float = 0.5               # 校准集占比（其余作测试）

    seed: int = 0

    def __post_init__(self):
        assert 0 < self.risk_alpha < 1
        assert 0 < self.calib_ratio < 1
