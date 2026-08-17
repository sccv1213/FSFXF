"""键路由:方向键按悬停目标分发的唯一实现(收敛旧版四重机制)。

旧版:QShortcut 全局秒跳 + 主窗口应用级 eventFilter(ShortcutOverride/
KeyPress 双阶段悬停分发)+ FrameView.event() ShortcutOverride 抢键 +
FrameView.keyPressEvent 兜底——四者配合极其隐晦,注释反复强调
"不能用 QApplication.sendEvent 会重入递归"。
新版:
- 空格/秒跳保持 QShortcut(ApplicationShortcut)不动(唯一真全局键)
- 方向键悬停分发收敛到 KeyRouter 单实例;悬停目标由 AppState.hover_target
  统一跟踪(FrameView/音量条 enter/leave 事件写入,替代 widgetAt 实时查)
- 微调 = 直接调 FrameView.handle_key(绝不用 sendEvent,防重入递归)
- 输入框天然有 ShortcutOverride 保护(光标移动),KeyRouter 放行即可
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt

from state.app_state import AppState
from .frame_view import FrameView


class KeyRouter(QObject):
    """app-level eventFilter 单实例:方向键按悬停目标分发。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state: AppState | None = None
        self._frame_view: FrameView | None = None
        self._volume_cb = None     # (delta) 音量增减回调

    def install(self, app, state: AppState, frame_view: FrameView, volume_cb) -> None:
        self._state = state
        self._frame_view = frame_view
        self._volume_cb = volume_cb
        app.installEventFilter(self)

    def uninstall(self, app) -> None:
        app.removeEventFilter(self)

    def eventFilter(self, obj, e) -> bool:
        if e.type() not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            return False
        key = e.key()
        if key not in (Qt.Key.Key_Left, Qt.Key.Key_Right,
                       Qt.Key.Key_Up, Qt.Key.Key_Down):
            return False
        target = self._state.hover_target if self._state is not None else "window"
        nudge_active = (self._frame_view is not None
                        and self._frame_view.nudge_arrows_active())
        if target == "volume" and key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            # 悬停音量条:左右键调音量 ±5(↑/↓ 放行)
            if e.type() == QEvent.Type.ShortcutOverride:
                e.accept()     # 阻止全局秒跳 QShortcut 抢键
                return True
            if self._volume_cb is not None:
                self._volume_cb(-5 if key == Qt.Key.Key_Left else 5)
            return True
        if target == "frame" and nudge_active:
            # 悬停画面 + 微调激活:方向键归微调所有
            if e.type() == QEvent.Type.ShortcutOverride:
                e.accept()     # 阻止全局秒跳 QShortcut 抢键
                return True
            return self._frame_view.handle_key(key, e.modifiers())
        return False   # 其他:放行(输入框自带保护/秒跳 QShortcut/FrameView 焦点级拦截)
