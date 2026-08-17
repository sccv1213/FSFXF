"""批量队列 UI:数据驱动(set_jobs)+ 状态色表(QColor 对象)+ 日志(500 块上限)。

实际队列逻辑在 RenderController;本组件只渲染 AppState.queue_jobs 快照。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .style import JOB_COLORS

_STATUS_TEXT = {
    "queued": "排队中", "running": "处理中", "ok": "完成", "failed": "失败",
    "cancelled": "已取消", "retry_pending": "等待选择…",
}


class QueueWidget(QWidget):
    addProjectRequested = Signal()
    batchImportRequested = Signal()      # 批量导入视频(按当前工程设置)
    removeSelectedRequested = Signal()
    startQueueRequested = Signal()
    cancelCurrentRequested = Signal()
    cancelAllRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        row = QHBoxLayout()
        row.addWidget(QLabel("批量队列:"))
        btn_add = QPushButton("添加当前工程")
        btn_add.clicked.connect(self.addProjectRequested)
        btn_batch = QPushButton("批量导入视频")
        btn_batch.setToolTip("按当前工程的补丁/复制/网格/设置批量处理多个视频(需无分段)")
        btn_batch.clicked.connect(self.batchImportRequested)
        btn_remove = QPushButton("移除选中")
        btn_remove.clicked.connect(self.removeSelectedRequested)
        btn_start = QPushButton("开始处理")
        btn_start.clicked.connect(self.startQueueRequested)
        btn_cancel1 = QPushButton("取消当前")
        btn_cancel1.clicked.connect(self.cancelCurrentRequested)
        btn_cancel_all = QPushButton("取消全部")
        btn_cancel_all.clicked.connect(self.cancelAllRequested)
        btn_log = QPushButton("日志")
        btn_log.setCheckable(True)
        btn_log.toggled.connect(self._toggle_log)
        for b in (btn_add, btn_batch, btn_remove, btn_start, btn_cancel1, btn_cancel_all, btn_log):
            row.addWidget(b)
        row.addStretch(1)   # 行尾 stretch:防剩余空间平均拉宽所有按钮(布局坑)
        lay.addLayout(row)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["状态", "视频", "进度"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self._table, 1)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(500)
        self._log.setMinimumHeight(90)
        self._log.hide()
        lay.addWidget(self._log, 1)

        self._rows: dict[str, int] = {}   # job_id → 行号

    # ---------- 数据驱动 ----------
    def set_jobs(self, jobs, current_job_id: str | None = None) -> None:
        """全量快照渲染(状态机事件驱动,简单可靠;行数少,无性能压力)。"""
        ids = [j.id for j in jobs]
        # 删除已不在队列的行(倒序删,避免行号位移)
        for jid in [j for j in self._rows if j not in ids]:
            row = self._rows.pop(jid)
            self._table.removeRow(row)
            for k, r in self._rows.items():
                if r > row:
                    self._rows[k] = r - 1
        for j in jobs:
            row = self._rows.get(j.id)
            if row is None:
                row = self._table.rowCount()
                self._table.insertRow(row)
                self._rows[j.id] = row
                self._table.setItem(row, 0, QTableWidgetItem())
                self._table.setItem(row, 1, QTableWidgetItem())
                bar = QProgressBar()
                # 鼠标穿透:点击进度条区域也能选中整行(否则点进度条列选不中)
                bar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                self._table.setCellWidget(row, 2, bar)
            self._update_row(j, row)

    def _update_row(self, job, row: int) -> None:
        st = job.status.value
        self._table.item(row, 0).setText(_STATUS_TEXT.get(st, st))
        self._table.item(row, 0).setForeground(JOB_COLORS.get(st, QColor(150, 150, 150)))
        name = job.project.video_path.rsplit("\\", 1)[-1] if job.project.video_path else ""
        self._table.item(row, 1).setText(name)
        self._table.item(row, 1).setToolTip(job.message)
        bar = self._table.cellWidget(row, 2)
        if isinstance(bar, QProgressBar):
            bar.setValue(int(job.progress * 100) if job.progress > 0 else 0)
            if st == "ok":
                bar.setValue(100)

    # ---------- 交互 ----------
    def selected_row_job(self) -> str | None:
        for item in self._table.selectedItems():
            for jid, r in self._rows.items():
                if r == item.row():
                    return jid
        return None

    # ---------- 日志 ----------
    def append_log(self, text: str) -> None:
        self._log.appendPlainText(text)

    def _toggle_log(self, on: bool) -> None:
        self._log.setVisible(on)
