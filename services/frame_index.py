"""FrameIndex 构建器:遍历视频 packet 收集真实 pts(不解码)。

VFR/规则 timebase 视频的帧号 ↔ 时间映射唯一来源。
构建在后台线程执行,避免打开视频时阻塞 UI。
"""
from __future__ import annotations

from fractions import Fraction

from PySide6.QtCore import QThread, Signal

from core.deps import PIP_INSTALL_HINT
from core.frame_index import FrameIndex

try:
    import av
except ImportError:  # pragma: no cover
    av = None


def build_frame_index(path: str) -> FrameIndex:
    """demux 视频 packet,按 PTS 排序得到显示顺序帧索引。"""
    if av is None:
        raise RuntimeError(f"未安装 PyAV,无法建立帧索引。请安装:{PIP_INSTALL_HINT}")
    with av.open(path) as container:
        stream = container.streams.video[0]
        pts: list[int] = []
        last_duration = 0
        for packet in container.demux(stream):
            if packet.pts is not None:
                pts.append(packet.pts)
            if packet.duration is not None:
                last_duration = max(last_duration, packet.duration)
        if not pts:
            raise RuntimeError("视频没有可索引的帧")
        duration = stream.duration or 0
        if duration <= 0 and pts:
            duration = pts[-1] + last_duration
        tb = stream.time_base if stream.time_base is not None else Fraction(1, 90000)
        return FrameIndex(pts, tb, duration)


class FrameIndexer(QThread):
    """后台建立帧索引。"""

    indexReady = Signal(object)
    indexFailed = Signal(str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path

    def run(self) -> None:
        try:
            self.indexReady.emit(build_frame_index(self._path))
        except Exception as e:
            self.indexFailed.emit(str(e))
