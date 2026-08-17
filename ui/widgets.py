"""通用小组件与格式化函数(迁移自 main_window 内联)。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMessageBox, QSpinBox


class ScaledPixmapLabel(QLabel):
    """等比缩放显示 pixmap:预览窗口拖动时画面跟随变化(QLabel.setPixmap 固定尺寸)。"""

    def __init__(self, pm, parent=None):
        super().__init__(parent)
        self._pm = pm
        self.setMinimumSize(240, 150)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def paintEvent(self, e) -> None:
        from PySide6.QtGui import QPainter
        p = QPainter(self)
        try:
            pm = self._pm
            if pm.isNull():
                return
            pm = pm.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
            x = (self.width() - pm.width()) // 2
            y = (self.height() - pm.height()) // 2
            p.drawPixmap(x, y, pm)
        finally:
            p.end()


class PaddedSpin(QSpinBox):
    """时间跳转输入框:无上下箭头(NoButtons + QSS width:0 双保险防残留宽度)。"""

    def __init__(self, maxv: int, parent=None):
        super().__init__(parent)
        self.setRange(0, maxv)
        self.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.setStyleSheet("QSpinBox::up-button, QSpinBox::down-button { width: 0px; }")

    def textFromValue(self, v: int) -> str:
        return f"{v:02d}"


def show_error(parent, title: str, msg: str) -> None:
    """统一错误弹窗(带详情展开)。"""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(msg)
    box.setStyleSheet(f"QPushButton {{ background: #2c2c2c; border: 1px solid #3a3a3a;"
                      f"border-radius: 4px; padding: 4px 12px; }}"
                      f"QPushButton:hover {{ background: #353535; }}")
    box.exec()


# ---- 时间格式化 ----
def fmt_hmsf(t: float, fps: float) -> str:
    """帧格式 hh:mm:ss:ff(帧号从 1 开始)。"""
    if fps <= 0:
        fps = 30.0
    total_ms = max(0, int(round(t * fps)))
    frames = total_ms % int(round(fps))
    total_s = total_ms // int(round(fps))
    return (f"{total_s // 3600:02d}:{total_s % 3600 // 60:02d}:"
            f"{total_s % 60:02d}:{frames + 1:02d}")


def fmt_hms(t: float) -> str:
    """h:mm:ss(时间轴/片段标签)。"""
    t = max(0, int(t))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"
