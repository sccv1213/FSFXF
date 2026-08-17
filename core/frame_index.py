"""视频帧索引:显示帧号 ↔ 真实 PTS 映射(纯数据,零 Qt/PyAV)。

VFR 下帧号不能再按 `frame / fps` 换算时间;本索引保存每帧真实 pts。
CFR/规则 timebase(例如 1/16000)同样受益:避免名义 60fps 与实际
packet pts 之间的累计漂移。
"""
from __future__ import annotations

from bisect import bisect_left
from fractions import Fraction


class FrameIndex:
    """按显示顺序排列的帧 pts 索引。"""

    def __init__(self, pts: list[int] | tuple[int, ...],
                 time_base: Fraction | tuple[int, int],
                 duration_ticks: int | None = None):
        if time_base is None:
            raise ValueError("time_base 不能为空")
        if isinstance(time_base, tuple):
            time_base = Fraction(time_base[0], time_base[1])
        pts = tuple(sorted(set(pts)))   # 去重:异常素材重复 PTS 不虚增帧数
        if duration_ticks is None or duration_ticks <= 0:
            duration_ticks = (pts[-1] if pts else 0)
        self.pts = pts
        self.time_base = time_base
        self.duration_ticks = duration_ticks

    # ---- 查询 ----
    @property
    def frame_count(self) -> int:
        return len(self.pts)

    def seconds(self, frame: int) -> float:
        """第 frame 帧(0 基)的真实显示时间。"""
        if not self.pts:
            return 0.0
        frame = max(0, min(frame, len(self.pts) - 1))
        return float(self.pts[frame] * self.time_base)

    def pts_ticks(self, frame: int) -> int:
        if not self.pts:
            return 0
        return self.pts[max(0, min(frame, len(self.pts) - 1))]

    def end_ticks(self, end_frame: int) -> int:
        """半开区间 [start, end) 的结束 pts。

        end_frame < frame_count → 下一帧的 pts(段结束不含该帧);
        end_frame == frame_count → 整个视频流 duration。
        """
        if end_frame < len(self.pts):
            return self.pts[end_frame]
        return self.duration_ticks

    def nearest_frame(self, t: float) -> int:
        """真实时间 t 最近的帧号(0 基)。"""
        if not self.pts:
            return 0
        ticks = int(round(t / float(self.time_base)))
        pos = bisect_left(self.pts, ticks)
        if pos == 0:
            return 0
        if pos >= len(self.pts):
            return len(self.pts) - 1
        left, right = self.pts[pos - 1], self.pts[pos]
        return pos - 1 if abs(left - ticks) <= abs(right - ticks) else pos

    def frame_at_or_after(self, t: float) -> int:
        """半开区间边界:第一帧 pts >= t 的帧号。

        范围起点含该帧;范围终点作为 [start,end) 的 end。
        t 在最后一帧之后(例如容器时长)返回 frame_count。
        """
        if not self.pts:
            return 0
        ticks = int(round(t / float(self.time_base)))
        return min(len(self.pts), bisect_left(self.pts, ticks))

    def frame_duration_ticks(self, frame: int) -> int:
        """该帧到下一帧的时间间隔;最后一帧用流 duration 补足。"""
        if not self.pts:
            return 0
        frame = max(0, min(frame, len(self.pts) - 1))
        if frame + 1 < len(self.pts):
            return self.pts[frame + 1] - self.pts[frame]
        return max(0, self.duration_ticks - self.pts[frame])

    def __len__(self) -> int:
        return len(self.pts)

    def __repr__(self) -> str:
        return f"FrameIndex({len(self.pts)} 帧, tb={self.time_base})"
