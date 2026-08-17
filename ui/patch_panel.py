"""右栏面板:补丁/复制/片段三页(迁移自旧 patch_panel.py,语义 1:1)。

刷新路径(决策 17③):来源/目标格互斥变化走 sync_patch_combos/
sync_copy_combos 局部刷新(整表重建会销毁弹出中的下拉);勾选变化走
scope_changed → set_scope_context 整表重建(勾选时鼠标不在下拉中,安全)。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHeaderView,
                               QLabel, QPushButton, QTabWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from core.planning import Scope
from core.project import Project
from .check_combo import CheckCombo
from .style import COL_DST
from .widgets import fmt_hms

# 复制规则表列索引唯一真源(新增"左右翻转"列,禁止散落魔法数字)
C_COPY_ENABLE = 0
C_COPY_NAME = 1
C_COPY_SOURCE = 2
C_COPY_TARGET = 3
C_COPY_FLIP = 4
C_COPY_DELETE = 5

# 补丁表/片段表列索引同样集中定义
C_PATCH_ENABLE = 0
C_PATCH_NAME = 1
C_PATCH_SOURCE = 2
C_PATCH_TARGET = 3
C_PATCH_LOCK = 4
C_PATCH_DELETE = 5

C_SEG_LABEL = 0
C_SEG_TIME = 1
C_SEG_PATCH_COUNT = 2
C_SEG_COPY_COUNT = 3
C_SEG_DELETE = 4


class PatchPanel(QWidget):
    """补丁表 + 复制规则表 + 片段表。所有操作发出意图信号,由主窗口执行模型变更。"""

    patchToggled = Signal(str, bool)        # patch id, 是否在当前片段启用
    patchSourceChanged = Signal(str, int)   # patch id, 来源格索引
    patchTargetsChanged = Signal(str, int, list)   # patch id, 主目标格, 额外目标格列表
    patchLockChanged = Signal(str, bool)    # patch id, 锁定对齐
    patchDeleted = Signal(str)              # patch id
    segmentDeleted = Signal(int)            # 片段在 project.segments 中的索引
    # ---- 复制规则 ----
    copyToggled = Signal(str, bool)         # rule id, 是否在当前片段启用
    copySourceChanged = Signal(str, int)    # rule id, 来源格
    copyTargetsChanged = Signal(str, list)  # rule id, 目标格列表
    copyFlipChanged = Signal(str, bool)     # rule id, 左右翻转
    copyDeleted = Signal(str)               # rule id

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        # ---- 补丁页(补丁表 + 复制规则表,同页上下排列) ----
        patch_widget = QWidget()
        pl = QVBoxLayout(patch_widget)
        pl.setContentsMargins(4, 4, 4, 4)
        self._patch_table = QTableWidget(0, 6)
        self._patch_table.setHorizontalHeaderLabels(
            ["启用", "补丁", "来源格", "目标格", "锁定对齐", ""])
        self._patch_table.verticalHeader().setVisible(False)
        self._patch_table.verticalHeader().setDefaultSectionSize(26)
        # 固定窄列(启用/补丁序号/锁定/删除),剩余空间给来源格/目标格
        fm = self._patch_table.fontMetrics()
        header = self._patch_table.horizontalHeader()
        for col, text in ((C_PATCH_ENABLE, "启用"), (C_PATCH_NAME, "补丁10"),
                          (C_PATCH_LOCK, "锁定对齐"), (C_PATCH_DELETE, "删除")):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self._patch_table.setColumnWidth(col, fm.horizontalAdvance(text) + 26)
        header.setSectionResizeMode(C_PATCH_SOURCE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(C_PATCH_TARGET, QHeaderView.ResizeMode.Stretch)
        self._patch_table.setMinimumHeight(180)
        pl.addWidget(self._patch_table)
        hint0 = QLabel("在画面上画目标/源矩形生成补丁,勾选 = 在当前时间所在片段生效。")
        hint0.setWordWrap(True)
        hint0.setObjectName("dim")
        pl.addWidget(hint0)
        self._copy_table = QTableWidget(0, 6)
        self._copy_table.setHorizontalHeaderLabels(
            ["启用", "复制", "来源格", "目标格", "左右翻转", ""])
        self._copy_table.verticalHeader().setVisible(False)
        self._copy_table.verticalHeader().setDefaultSectionSize(26)
        header = self._copy_table.horizontalHeader()
        fm = self._copy_table.fontMetrics()
        for col, text in ((C_COPY_ENABLE, "启用"), (C_COPY_NAME, "复制10"),
                          (C_COPY_FLIP, "左右翻转"), (C_COPY_DELETE, "删除")):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self._copy_table.setColumnWidth(col, fm.horizontalAdvance(text) + 26)
        header.setSectionResizeMode(C_COPY_SOURCE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(C_COPY_TARGET, QHeaderView.ResizeMode.Stretch)
        self._copy_table.setMinimumHeight(160)
        pl.addWidget(self._copy_table)
        hint2 = QLabel("复制格子:把来源格整格内容复制到目标格(可多选,不含来源格)。\n"
                       "勾选 = 在当前时间所在片段生效。")
        hint2.setWordWrap(True)
        hint2.setObjectName("dim")
        pl.addWidget(hint2)
        tabs.addTab(patch_widget, "补丁")

        # ---- 片段页(分段线列表:每条用户分段线一行,删除按钮在线行上;
        #        首尾永久分段线显示但不可删除;分割按钮在工具栏) ----
        seg_widget = QWidget()
        sl = QVBoxLayout(seg_widget)
        sl.setContentsMargins(4, 4, 4, 4)
        self._seg_table = QTableWidget(0, 5)
        self._seg_table.setHorizontalHeaderLabels(["分段线", "时间", "补丁数", "复制数", ""])
        self._seg_table.verticalHeader().setVisible(False)
        self._seg_table.verticalHeader().setDefaultSectionSize(26)
        header = self._seg_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._seg_table.setColumnWidth(C_SEG_DELETE, 64)
        sl.addWidget(self._seg_table)
        hint = QLabel("分段行显示该段的补丁/复制数;分段线 = 片段边界,删除一条线\n"
                      "会合并其两侧片段并清空补丁/复制勾选。开头/结尾的永久分段线\n"
                      "不可删除。工具栏\"分割\"按钮 = 在播放头位置添加分段线。")
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        sl.addWidget(hint)
        tabs.addTab(seg_widget, "片段")

        # 补丁/复制条目不需要选中交互(用户要求)——操作全在行内控件,
        # 禁用选中后未启用红字永不被选中样式覆盖
        self._patch_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._copy_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

        self._project: Project | None = None
        self._cur_scope: Scope | None = None
        self._tabs = tabs

    # ---------- 数据刷新 ----------
    def set_scope_context(self, scope: Scope | None) -> None:
        """推送当前 scope(主窗口在 scope_changed 时调用):勾选集合 + 网格格数。"""
        self._cur_scope = scope
        self.refresh_patches()
        self.refresh_copy_rules()

    def set_project(self, project: Project | None) -> None:
        self._project = project
        self.refresh_patches()
        self.refresh_segments()

    def switch_tab(self, name: str) -> None:
        """切到指定标签页:segments(片段页 1)/ 其他(补丁页 0,复制已并入)。"""
        if name == "segments":
            self._tabs.setCurrentIndex(1)
        else:
            self._tabs.setCurrentIndex(0)

    # ---------- 来源格/目标格互斥同步(不重建表格——重建会销毁弹出中的下拉) ----------
    def sync_patch_combos(self, pid: str) -> None:
        """补丁来源/目标格互斥:目标格禁用来源格;来源格禁用目标格集合。"""
        p = self._project.patch(pid) if self._project else None
        if p is None:
            return
        row = next((i for i, x in enumerate(self._project.patches) if x.id == pid), None)
        if row is None:
            return
        src = self._patch_table.cellWidget(row, C_PATCH_SOURCE)
        dst = self._patch_table.cellWidget(row, C_PATCH_TARGET)
        # 互斥:来源格禁用已勾选目标格;目标格禁用来源格(与复制页一致)
        if isinstance(src, CheckCombo):
            src.set_disabled(p.target_tile_indices())
        if isinstance(dst, CheckCombo):
            dst.set_disabled([p.source_tile_idx])
            dst.set_checked([p.dst_tile_idx] + list(p.extra_tile_indices))

    def sync_copy_combos(self, rid: str) -> None:
        """复制规则来源/目标格互斥(同 sync_patch_combos)。

        注意列索引:复制表 [启用, 复制, 来源格, 目标格, 删除]——来源格 col 2、
        目标格 col 3(曾用 col 1/2,加"复制"序号列后未同步导致互斥失效)。
        """
        r = self._project.copy_rule(rid) if self._project else None
        if r is None:
            return
        row = next((i for i, x in enumerate(self._project.copy_rules) if x.id == rid), None)
        if row is None:
            return
        src = self._copy_table.cellWidget(row, C_COPY_SOURCE)
        dst = self._copy_table.cellWidget(row, C_COPY_TARGET)
        if isinstance(src, CheckCombo):
            src.set_disabled(list(r.target_tile_indices))
        if isinstance(dst, CheckCombo):
            dst.set_disabled([r.source_tile_idx])
            dst.set_checked(list(r.target_tile_indices))

    # ---------- 复制规则表 ----------
    def refresh_copy_rules(self) -> None:
        proj = self._project
        self._copy_table.setRowCount(len(proj.copy_rules) if proj else 0)
        if not proj:
            return
        enabled = set(self._cur_scope.copy_rule_ids) if self._cur_scope else set()
        # 未被任何段启用的复制序号标红(全局模式全部生效 → 默认色)
        enabled_anywhere = ({rid for seg in proj.segments for rid in seg.copy_rule_ids}
                            if proj.segments else None)
        for row, r in enumerate(proj.copy_rules):
            rule_grid = proj.resolve_grid(r.anchor_grid)
            grid_count = len(rule_grid.tiles) if rule_grid.tiles else 0
            cb = QCheckBox()
            cb.setChecked(r.id in enabled)
            cb.setToolTip("该规则是否在当前时间所在片段生效")
            cb.stateChanged.connect(
                lambda st, rid=r.id: self.copyToggled.emit(rid, bool(st)))
            self._copy_table.setCellWidget(row, C_COPY_ENABLE, cb)
            idx_item = QTableWidgetItem(f"复制{row + 1}")
            if enabled_anywhere is not None and r.id not in enabled_anywhere:
                idx_item.setForeground(COL_DST)
            self._copy_table.setItem(row, C_COPY_NAME, idx_item)
            src_combo = CheckCombo(
                grid_count, [r.source_tile_idx], list(r.target_tile_indices),
                multi=False, placeholder="来源格…",
                on_change=lambda checked, rid=r.id: self.copySourceChanged.emit(
                    rid, checked[0] if checked else -1))
            self._copy_table.setCellWidget(row, C_COPY_SOURCE, src_combo)
            dst_combo = CheckCombo(
                grid_count, list(r.target_tile_indices), [r.source_tile_idx],
                multi=True, placeholder="目标格…",
                on_change=lambda checked, rid=r.id: self.copyTargetsChanged.emit(
                    rid, checked))
            self._copy_table.setCellWidget(row, C_COPY_TARGET, dst_combo)
            flip_cb = QCheckBox("左右翻转")
            flip_cb.setChecked(r.flip_horizontal)
            flip_cb.setToolTip("勾选后该复制规则的目标格画面左右镜像")
            flip_cb.stateChanged.connect(
                lambda st, rid=r.id: self.copyFlipChanged.emit(rid, bool(st)))
            self._copy_table.setCellWidget(row, C_COPY_FLIP, flip_cb)
            btn_del = QPushButton("删除")
            btn_del.clicked.connect(lambda _, rid=r.id: self.copyDeleted.emit(rid))
            self._copy_table.setCellWidget(row, C_COPY_DELETE, btn_del)

    def refresh_patches(self) -> None:
        """整表重建(结构变化时);勾选/互斥变化走 sync_patch_combos 不调这里。"""
        proj = self._project
        self._patch_table.setRowCount(len(proj.patches) if proj else 0)
        if not proj:
            return
        enabled = set(self._cur_scope.patch_ids) if self._cur_scope else set()
        # 未被任何段启用的补丁序号标红(全局模式全部生效 → 默认色)
        enabled_anywhere = ({pid for seg in proj.segments for pid in seg.patch_ids}
                            if proj.segments else None)
        for row, p in enumerate(proj.patches):
            patch_grid = proj.resolve_grid(p.anchor_grid)
            grid_count = len(patch_grid.tiles) if patch_grid.tiles else 0
            cb = QCheckBox()
            cb.setChecked(p.id in enabled)
            cb.setToolTip("该补丁是否在当前时间所在片段生效")
            cb.stateChanged.connect(
                lambda st, pid=p.id: self.patchToggled.emit(pid, bool(st)))
            self._patch_table.setCellWidget(row, C_PATCH_ENABLE, cb)
            item = QTableWidgetItem(f"补丁{row + 1} 目标({p.dst.nx:.2f},{p.dst.ny:.2f})")
            if enabled_anywhere is not None and p.id not in enabled_anywhere:
                item.setForeground(COL_DST)
            self._patch_table.setItem(row, C_PATCH_NAME, item)
            # 来源格禁用已勾选的目标格(用户要求互斥:来源格下拉中目标格灰显不可选)
            src_combo = CheckCombo(
                grid_count, [p.source_tile_idx], list(p.target_tile_indices()),
                multi=False, placeholder="来源格…",
                on_change=lambda checked, pid=p.id: self.patchSourceChanged.emit(
                    pid, checked[0] if checked else -1))
            self._patch_table.setCellWidget(row, C_PATCH_SOURCE, src_combo)
            dst_combo = CheckCombo(
                grid_count, [p.dst_tile_idx] + list(p.extra_tile_indices),
                [p.source_tile_idx],
                multi=True, placeholder="目标格…",
                on_change=lambda checked, pid=p.id: self.patchTargetsChanged.emit(
                    pid, checked[0] if checked else -1,
                    checked[1:] if checked else []))
            self._patch_table.setCellWidget(row, C_PATCH_TARGET, dst_combo)
            lock = QCheckBox()
            lock.setChecked(p.lock_align)
            lock.setEnabled(grid_count > 0)
            lock.setToolTip("锁定:源/目标矩形随格子自动对齐")
            lock.stateChanged.connect(
                lambda st, pid=p.id: self.patchLockChanged.emit(pid, bool(st)))
            self._patch_table.setCellWidget(row, C_PATCH_LOCK, lock)
            btn = QPushButton("删除")
            btn.clicked.connect(lambda _, pid=p.id: self.patchDeleted.emit(pid))
            self._patch_table.setCellWidget(row, C_PATCH_DELETE, btn)

    def refresh_segments(self) -> None:
        """片段页 = 分段与分段线交替排列:

        永久(开头) / 分段1(补丁数·复制数) / 分段线1 / 分段2 / 分段线2 /
        ... / 永久(结尾)。分段行显示补丁/复制数;分段线行不显示;
        删除按钮在分段线上;永久线不可删除。
        """
        proj = self._project
        if not proj:
            self._seg_table.setRowCount(0)
            return
        lo, hi = proj.process_range
        segs = sorted(proj.segments, key=lambda s: s.start)
        rows = [("perm_start", lo, None)]
        for i, s in enumerate(segs):
            rows.append(("seg", s.start, s))
            if i < len(segs) - 1:
                rows.append(("line", segs[i + 1].start, None))   # 分段线 = 下一段起点
        rows.append(("perm_end", hi, None))
        self._seg_table.setRowCount(len(rows))
        seg_no = 0
        for row, (kind, t, seg) in enumerate(rows):
            if kind == "seg":
                seg_no += 1
                label = f"分段{seg_no}"
                # 显示时间随处理范围 clamp(范围变化时首段/末段跟随开头/结尾)
                show_start = max(seg.start, lo)
                show_end = min(seg.end, hi)
                time_text = ("范围外" if show_end < show_start
                             else f"{fmt_hms(show_start)}-{fmt_hms(show_end)}")
                pc, cc = str(len(seg.patch_ids)), str(len(seg.copy_rule_ids))
            elif kind == "line":
                label = f"分段线{seg_no}"     # 分段 seg_no 与 seg_no+1 之间的边界
                time_text = fmt_hms(t)
                pc = cc = "—"
            elif kind == "perm_start":
                label = "开头"
                time_text = fmt_hms(t)
                pc = cc = "—"
            else:
                label = "结尾"
                time_text = fmt_hms(t)
                pc = cc = "—"
            self._seg_table.setItem(row, C_SEG_LABEL, QTableWidgetItem(label))
            self._seg_table.setItem(row, C_SEG_TIME, QTableWidgetItem(time_text))
            self._seg_table.setItem(row, C_SEG_PATCH_COUNT, QTableWidgetItem(pc))
            self._seg_table.setItem(row, C_SEG_COPY_COUNT, QTableWidgetItem(cc))
            if kind == "line":
                btn = QPushButton("删除")
                btn.setToolTip("删除该分段线:合并两侧片段并清空补丁/复制勾选、重置分界线")
                btn.clicked.connect(lambda _, tt=t: self._emit_segment_line_deleted(tt))
                self._seg_table.setCellWidget(row, C_SEG_DELETE, btn)
            else:
                # 先清残留 cellWidget:行重建时非 line 行可能继承上一轮 line 行
                # 的删除按钮(删除分段线后行数收缩、内容错位曾致按钮残留在
                # 分段/结尾行且闭包 t 仍指向已删除的线)
                self._seg_table.setCellWidget(row, C_SEG_DELETE, None)
                self._seg_table.setItem(row, C_SEG_DELETE, QTableWidgetItem("—"))

    # ---------- 表格交互 ----------
    def _emit_segment_line_deleted(self, t: float) -> None:
        """删除分段线 = 删除从该线开始的片段 → 合并两侧(remove_segment_merge)。"""
        if not self._project:
            return
        seg = self._project.segment_at(t)
        if seg is None:
            return
        self.segmentDeleted.emit(self._project.segments.index(seg))
