"""来源格(单选)/目标格(多选)可勾选下拉(迁移自旧 patch_panel._CheckCombo)。

这是踩坑的汇编,行为被 33 项 GUI 测试锁定,重写保留原样。陷阱(必须固化):
1. 事件过滤器必须同时装 view 和 view.viewport()——真实鼠标事件发送给
   QAbstractScrollArea 的 viewport,只装 view 从未被调用
2. Python 引用必须保存(过滤器对象挂在 combo 属性上防 GC;QTimer 同)
3. press 阶段拦截整个项区域手动 toggle(editable 下拉在 MouseButtonPress
   阶段就会关闭弹出,含 checkbox 区域)
4. 单选逐项 setCheckState 不能 blockSignals——会阻止 dataChanged →
   弹出视图不重绘,勾选要鼠标移开才显示
5. 勾选变化 150ms 防抖后回调 on_change(勾选索引列表)
6. 弹出层第一项上缘 2~3px 是容器边框/边距死区:indexAt 返回无效,点击
   整次丢失(用户报告"点第一项没反应,文字残留",递减切换 3→2→1 最后
   落到第一项时最明显)。过滤器扩展到弹出层容器 + 坐标夹进 viewport 后
   弹出层内任何点击必命中某行;弹出层窗口外点击放行由 Qt 关闭弹出。
"""
from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QModelIndex, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QComboBox

# 诊断开关:设置环境变量 FENSHENFU_DEBUG_COMBO=1 后,控制台版(python launcher.pyw)
# 运行会打印来源格/目标格下拉的点击命中与文字变化(定位真实 GUI 事件差异用)
_DEBUG = os.environ.get("FENSHENFU_DEBUG_COMBO") == "1"


def _dbg(*args) -> None:
    if _DEBUG:
        print("[combo]", *args, flush=True)


class _ComboFilter(QObject):
    """可勾选下拉事件过滤器:点击菜单项 = 勾选切换且不关闭弹出。

    过滤器装在弹出层容器 + view + viewport 三层(陷阱 1 的扩展):真实鼠标
    事件经窗口系统按命中分发给最顶层子控件,点击第一项上缘 2~3px 的容器
    边框/边距区会落在容器上而非 viewport——只装 view/viewport 时 indexAt
    返回无效,点击整次丢失(用户报告"点第一项没反应,文字残留")。坐标经
    全局位置归一化并夹进 viewport,弹出层内任何点击必命中某行(顶部=第一
    项、底部=末项);弹出层窗口外的点击放行,由 Qt 关闭弹出(Qt::Popup 语义)。
    """

    def __init__(self, combo: "_CheckCombo") -> None:
        super().__init__(combo.view())
        self._combo = combo
        for w in (combo.view().parentWidget(), combo.view(),
                  combo.view().viewport()):
            if w is not None:
                w.installEventFilter(self)

    def _index_at(self, e) -> QModelIndex:
        """事件坐标(容器/view/viewport 任一坐标系)→ viewport 坐标并夹取。

        夹取的意义:弹出层 2px 边框/边距与 DPI 分数像素缩放都会让
        `indexAt` 落空(点击第一项附近曾整次丢失);夹进 [0, viewport 尺寸)
        后必命中某行——顶部夹取命中第一项、底部命中末项。
        """
        vp = self._combo.view().viewport()
        local = e.globalPosition().toPoint() - vp.mapToGlobal(QPoint(0, 0))
        x = max(0, min(vp.width() - 1, local.x()))
        y = max(0, min(vp.height() - 1, local.y()))
        return self._combo.view().indexAt(QPoint(x, y))

    def _inside_popup(self, e) -> bool:
        """事件位置是否在弹出层窗口内(容器外点击放行给 Qt 关闭弹出)。"""
        c = self._combo.view().parentWidget()
        return (c is not None and c.window() is not None
                and c.window().rect().contains(
                    c.window().mapFromGlobal(e.globalPosition().toPoint())))

    def eventFilter(self, obj, e) -> bool:
        if e.type() == QEvent.Type.MouseButtonPress:
            if not self._inside_popup(e):
                return False
            idx = self._index_at(e)
            if idx.isValid():
                self._combo.toggle_row(idx.row())
                return True          # 拦截 press:不激活、不关闭弹出(含禁用项)
        elif e.type() == QEvent.Type.MouseButtonRelease:
            # release 也拦截(不重复 toggle):view 无 press 状态时 release
            # 仍会触发激活/关闭
            if not self._inside_popup(e):
                return False
            if self._index_at(e).isValid():
                return True
        return False


class CheckCombo(QComboBox):
    """来源格(单选)/目标格(多选)可勾选下拉。"""

    _DEBOUNCE_MS = 150

    def __init__(self, count: int, checked: list, disabled: list,
                 multi: bool, placeholder: str, on_change, parent=None):
        super().__init__(parent)
        self._multi = multi
        self._placeholder = placeholder
        self._on_change = on_change
        self._model = QStandardItemModel(self)
        for i in range(count):
            it = QStandardItem(f"格{i + 1}")
            it.setData(i)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if i in checked
                             else Qt.CheckState.Unchecked)
            if i in disabled:
                it.setEnabled(False)
            self._model.appendRow(it)
        self.setModel(self._model)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)     # 收起显示摘要而非单项
        self.setCurrentIndex(-1)
        self._update_text()
        self._debounce = QTimer(self)         # Python 属性持有引用(防 GC)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self._DEBOUNCE_MS)
        self._debounce.timeout.connect(self._fire)
        self._filter = _ComboFilter(self)     # Python 属性持有引用(防 GC)
        self._model.itemChanged.connect(self._on_item_changed)

    # ---- 查询 ----
    def checked(self) -> list:
        return [i for i in range(self._model.rowCount())
                if self._model.item(i).isEnabled()
                and self._model.item(i).checkState() == Qt.CheckState.Checked]

    # ---- 外部同步(模型变化后调用,不触发发射) ----
    def set_disabled(self, disabled: list) -> None:
        """互斥:对方已选的格子禁用。"""
        self._model.blockSignals(True)
        for i in range(self._model.rowCount()):
            self._model.item(i).setEnabled(i not in disabled)
        self._model.blockSignals(False)

    def set_checked(self, checked: list) -> None:
        """外部同步勾选状态(不触发防抖发射)。"""
        self._model.blockSignals(True)
        for i in range(self._model.rowCount()):
            it = self._model.item(i)
            want = Qt.CheckState.Checked if i in checked else Qt.CheckState.Unchecked
            if it.checkState() != want:
                it.setCheckState(want)
        self._model.blockSignals(False)
        self._update_text()

    # ---- 内部 ----
    def toggle_row(self, row: int) -> None:
        """点击菜单项:多选切换 / 单选切换(含禁用项无操作)。"""
        it = self._model.item(row)
        if it is None or not it.isEnabled():
            _dbg(f"toggle_row({row}) 忽略:禁用/无效 item")
            return
        _dbg(f"toggle_row({row}) 前 checked={self.checked()} "
             f"disabled={[i for i in range(self._model.rowCount()) if not self._model.item(i).isEnabled()]}")
        if self._multi:
            it.setCheckState(Qt.CheckState.Unchecked
                             if it.checkState() == Qt.CheckState.Checked
                             else Qt.CheckState.Checked)
        else:
            # 逐项 setCheckState(不能 blockSignals:会阻止 dataChanged →
            # 弹出视图不重绘,勾选要鼠标移开才显示)
            for r in range(self._model.rowCount()):
                other = self._model.item(r)
                want = Qt.CheckState.Checked if r == row else Qt.CheckState.Unchecked
                if other.checkState() != want:
                    other.setCheckState(want)
        # 文字由 itemChanged → _on_item_changed 统一刷新(防御层已收敛)
        _dbg(f"toggle_row({row}) 后 checked={self.checked()} text={self.lineEdit().text()!r}")

    def hidePopup(self) -> None:
        """弹出关闭时强制刷新摘要。

        真实 GUI 中 editable combo 的弹出层与控件本体存在事件重叠区:
        点击第一项附近可能命中 combo 本体 → 弹出被 Qt 关闭且 toggle 未
        执行(用户报告'递减切换到第一项文字残留')。关闭后刷新摘要,
        保证 lineEdit 文字恒等于当前勾选状态(不被 combo 内部 currentText
        写回覆盖)。offscreen 测试无法复现该事件差异,此为防御。
        """
        super().hidePopup()
        _dbg(f"hidePopup 刷新 text={self.lineEdit().text()!r}")
        self._update_text()

    def _on_item_changed(self, item) -> None:
        self._update_text()
        self._debounce.start()

    def _fire(self) -> None:
        _dbg(f"_fire checked={self.checked()}")
        self._on_change(self.checked())

    def _update_text(self) -> None:
        text = ",".join(f"格{i + 1}" for i in self.checked())
        self.lineEdit().setText(text or self._placeholder)
        _dbg(f"_update_text -> {self.lineEdit().text()!r} (checked={self.checked()})")
