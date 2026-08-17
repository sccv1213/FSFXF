"""统一视觉:QSS + 调色板(全部 QColor 对象——PySide6 6.11 不接受 str)。

所有颜色一律 QColor 对象;队列/补丁/复制等状态色在此集中定义。
"""
from __future__ import annotations

from PySide6.QtGui import QColor

# ---- 调色板(全部 QColor 对象) ----
BG = QColor(24, 24, 24)              # 画面画布背景
COL_DST = QColor(232, 64, 64)        # 目标(脏区域)红 / 未启用序号
COL_SRC = QColor(72, 200, 96)        # 源(干净区域)绿
COL_SEL = QColor(240, 210, 60)       # 选中黄
COL_GRID = QColor(140, 160, 200, 160)   # 网格虚线
COL_EDGE_IDLE = QColor(190, 190, 190, 200)   # 边缘线原位灰
COL_TEXT = QColor(200, 200, 200)

# 队列状态色表(6.11 不接受 str,统一 QColor 对象)
JOB_COLORS = {
    "queued": QColor(150, 150, 150),
    "running": QColor(66, 133, 244),
    "ok": QColor(80, 200, 120),
    "failed": QColor(232, 64, 64),
    "cancelled": QColor(120, 120, 120),
    "retry_pending": QColor(240, 160, 40),
}

# ---- QSS ----
QSS = """
QMainWindow, QWidget { background-color: #1e1e1e; color: #c8c8c8;
    font-size: 9.5pt; }
QWidget { font-size: 9.5pt; }
QLabel { color: #c8c8c8; }
QLabel#dim { color: #888888; }
QPushButton, QToolButton { background: #2c2c2c; border: 1px solid #3a3a3a;
    border-radius: 4px; padding: 4px 12px; min-height: 18px; }
QPushButton:hover, QToolButton:hover { background: #353535; }
QPushButton:checked, QToolButton:checked { background: #2a4a7a; border-color: #4285f4; }
QPushButton:disabled { color: #666; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #2c2c2c;
    border: 1px solid #3a3a3a; border-radius: 4px; padding: 2px 6px;
    min-height: 22px; selection-background-color: #4285f4; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled { color: #777777; background: #262626;
    text-decoration: line-through; }   /* 不生效的选项划掉(灰显兜底) */
QComboBox QAbstractItemView { background: #2c2c2c; border: 1px solid #3a3a3a;
    selection-background-color: #4285f4; }
QSlider::groove:horizontal { height: 4px; background: #3a3a3a; border-radius: 2px; }
QSlider::handle:horizontal { width: 12px; margin: -4px 0; border-radius: 6px;
    background: #4285f4; }
QTableWidget, QTableView { background: #262626; gridline-color: #333;
    border: 1px solid #333; selection-background-color: #3a5a9a; }
/* 补丁/复制表已禁用选中(NoSelection),未启用红字不会被选中样式覆盖;
   选中样式供片段表/队列表使用 */
QHeaderView::section { background: #2c2c2c; border: none;
    border-bottom: 1px solid #3a3a3a; padding: 4px; }
QTabWidget::pane { border: 1px solid #333; }
QTabBar::tab { background: #2c2c2c; padding: 5px 14px; border: 1px solid #333;
    border-bottom: none; }
QTabBar::tab:selected { background: #262626; border-top: 2px solid #4285f4; }
QTabBar::tab:hover { background: #353535; }
QStatusBar { background: #262626; border-top: 1px solid #333; }
QProgressBar { background: #2c2c2c; border: 1px solid #333; border-radius: 3px;
    text-align: center; }
QProgressBar::chunk { background: #4285f4; border-radius: 3px; }
QSplitter::handle { background: #333; }
QToolTip { background: #333; color: #ddd; border: 1px solid #555; }
"""
