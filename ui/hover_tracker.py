"""统一悬停上报:HoverTracker 用事件过滤器替代 lambda 替换 enterEvent/leaveEvent。

FrameView 自己在 enter/leave 中写 state;本类负责音量条等其余控件。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject


class HoverTracker(QObject):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self._state = state
        self._tracked = []

    def track(self, widget, target: str) -> None:
        """widget 进入时 hover_target=target,离开时回到 window。"""
        widget.installEventFilter(self)
        self._tracked.append((widget, target))

    def eventFilter(self, obj, e) -> bool:
        for widget, target in self._tracked:
            if obj is not widget:
                continue
            if e.type() == QEvent.Type.Enter:
                self._state.set_hover(target)
                return False
            if e.type() == QEvent.Type.Leave:
                self._state.set_hover("window")
                return False
        return False
