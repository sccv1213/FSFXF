"""时间轴:片段带 / 处理范围 / 播放头 / 拖动 seek(迁移自旧版)。

性能:播放头变化 ≥1px 才重绘(旧版每帧 repaint)。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from .widgets import fmt_hms

_BG = QColor(30, 30, 32)
_SEG_PATCH = QColor(86, 156, 240)      # 有补丁的片段
_SEG_EMPTY = QColor(120, 120, 124)     # 无补丁
_RANGE = QColor(46, 46, 52)
_PLAYHEAD = QColor(240, 120, 60)
_MARGIN = 10.0


class TimelineWidget(QWidget):
    seekRequested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(58)
        self.setMouseTracking(True)
        self._duration = 0.0
        self._range = (0.0, 0.0)
        self._segments: list = []       # 有效 scope 序列(含间隙补段)
        self._user_segments: list = []  # 用户片段(画起点标记用)
        self._playhead = 0.0
        self._dragging = False
        self._hover_t: float | None = None
        self._last_playhead_px = -1.0

    def set_data(self, duration: float, process_range, scopes, playhead: float,
                 user_segments=None) -> None:
        self._duration = duration
        self._range = tuple(process_range)
        self._segments = scopes
        self._user_segments = user_segments if user_segments is not None else []
        self._playhead = playhead
        self._last_playhead_px = -1.0
        self.update()

    def set_playhead(self, t: float) -> None:
        """播放头更新:位置变化 ≥1px 才重绘(性能)。"""
        self._playhead = t
        px = self._t2x(t)
        if abs(px - self._last_playhead_px) >= 1.0:
            self._last_playhead_px = px
            self.update()

    # ---------- 坐标 ----------
    def _t2x(self, t: float) -> float:
        if self._duration <= 0:
            return 0.0
        return _MARGIN + t / self._duration * (self.width() - 2 * _MARGIN)

    def _x2t(self, x: float) -> float:
        if self._duration <= 0:
            return 0.0
        t = (x - _MARGIN) / (self.width() - 2 * _MARGIN) * self._duration
        return max(0.0, min(self._duration, t))

    # ---------- 绘制 ----------
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), _BG)
        if self._duration <= 0:
            p.setPen(QColor(130, 130, 130))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "打开视频后显示时间轴")
            return
        w = self.width()
        lo, hi = self._range
        bar_y, bar_h = 20.0, 22.0
        # 处理范围带
        x_lo, x_hi = self._t2x(lo), self._t2x(hi)
        p.fillRect(QRectF(0, bar_y - 4, w, bar_h + 8), _RANGE)
        # 片段带(有补丁蓝 / 无补丁灰)
        p.setPen(Qt.PenStyle.NoPen)
        for seg in self._segments:
            x1, x2 = self._t2x(seg.start), self._t2x(seg.end)
            # 有补丁或复制规则都染色(修复:仅复制整格的分段曾显示灰色)
            color = _SEG_PATCH if seg.has_work else _SEG_EMPTY
            p.fillRect(QRectF(x1, bar_y, max(2.0, x2 - x1), bar_h), color)
        p.setPen(QPen(QColor(255, 255, 255, 40)))
        p.drawRect(QRectF(0, bar_y - 4, w - 1, bar_h + 8))
        # 刻度
        step = self._tick_step()
        p.setPen(QColor(160, 160, 160))
        first = int(lo // step) * step
        t = first
        while t <= hi + 1e-6:
            x = self._t2x(t)
            p.drawLine(int(x), bar_y + bar_h + 6, int(x), bar_y + bar_h + 11)
            p.drawText(int(x) - 30, bar_y + bar_h + 23, 60, 14,
                       Qt.AlignmentFlag.AlignHCenter, fmt_hms(t))
            t += step
        # 首尾永久分段条(灰色):直接作为第一个分段的开头 / 最后一个分段的结尾——
        # 在 lo/hi 处画贯穿段带的边界线(段带左右边缘),保留顶部小三角锚点。
        # 随处理范围设置移动;无用户分段时即默认全视频分段的边界。
        edge_col = QColor(150, 150, 150, 170)
        p.setPen(QPen(edge_col, 2))
        p.setBrush(edge_col)
        for edge_t in (self._range[0], self._range[1]):
            x = self._t2x(edge_t)
            p.drawLine(int(x), bar_y, int(x), bar_y + bar_h)   # 贯穿段带 = 段边界
            p.drawLine(int(x), 2, int(x), bar_y - 6)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygonF([QPointF(int(x), 2), QPointF(int(x) - 4, 9),
                                     QPointF(int(x) + 4, 9)]))
            p.setPen(QPen(edge_col, 2))
        # 用户片段起点标记(竖线 + 顶部小三角)
        for seg in self._user_segments:
            if seg.start <= self._range[0] + 1e-6:
                continue   # 处理范围起点处只显示永久条,不画用户分段线(分割仅生成光标处线段)
            x = self._t2x(seg.start)
            p.setPen(QPen(_SEG_PATCH, 2))
            p.drawLine(int(x), 2, int(x), bar_y - 6)
            p.setBrush(_SEG_PATCH)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygonF([QPointF(int(x), 2), QPointF(int(x) - 4, 9),
                                     QPointF(int(x) + 4, 9)]))
        # 播放头
        x = self._t2x(self._playhead)
        p.setPen(QPen(_PLAYHEAD, 2))
        p.drawLine(int(x), 4, int(x), self.height() - 4)
        # 悬停时间(左下角):鼠标指向或拖动播放头时显示,移出隐藏
        if self._hover_t is not None:
            p.setPen(QColor(220, 220, 220))
            p.drawText(8, self.height() - 16, 90, 14, Qt.AlignmentFlag.AlignLeft,
                       fmt_hms(self._hover_t))

    def _tick_step(self) -> float:
        """自适应刻度步长。"""
        span = self._range[1] - self._range[0]
        px_per_sec = (self.width() - 20) / max(span, 1)
        for step in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200):
            if step * px_per_sec >= 90:
                return step
        return 7200

    # ---------- 鼠标 ----------
    def mousePressEvent(self, e) -> None:
        if self._duration <= 0:
            return
        self._dragging = True
        t = self._x2t(e.position().x())
        self.set_playhead(t)
        self.seekRequested.emit(t)

    def mouseMoveEvent(self, e) -> None:
        self._hover_t = self._x2t(e.position().x())
        if self._dragging:
            t = self._x2t(e.position().x())
            self.set_playhead(t)
            self.seekRequested.emit(t)
        else:
            self.update()   # 悬停时间标签刷新

    def leaveEvent(self, e) -> None:
        # 悬停时间只在鼠标指向时间轴时显示,离开即隐藏
        # (注意:Qt 离开事件是 leaveEvent,mouseLeaveEvent 不是虚方法不会被调用)
        self._hover_t = None
        self.update()
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        self._dragging = False
        # Qt 按下期间 leave 不触发:拖出时间轴松开后悬停时间会残留 → 越界清理
        if not self.rect().contains(e.position().toPoint()):
            self._hover_t = None
        self.update()
