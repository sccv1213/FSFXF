"""补丁/复制规则编辑命令:MainWindow 只转发信号,业务逻辑集中于此。"""
from __future__ import annotations

from core.project import CopyRule, Rect


class EditController:
    """依赖 win 的公开协作对象(不直接 import MainWindow,避免循环)。"""

    def __init__(self, win):
        self._win = win

    # ---- 工具 ----
    @staticmethod
    def tile_label(idx: int) -> str:
        return f"格{idx + 1}" if idx >= 0 else "手动"

    def _refresh_copy_ui(self, rid: str) -> None:
        win = self._win
        win._frame_view.set_copy_highlight(win._copy_highlight())
        win._patch_panel.sync_copy_combos(rid)
        win._frame_view.refresh()

    def _project(self):
        return self._win._state.project

    # ---- 补丁 ----
    def patch_created(self, patch) -> None:
        win = self._win
        win._state.add_patch(patch)
        win._frame_view.select_patch(patch.id)
        win._state_label.setText(
            f"已添加补丁(源格{self.tile_label(patch.source_tile_idx)} → 目标)")

    def patch_toggled(self, pid: str, on: bool) -> None:
        self._win._state.toggle_patch(pid, on)

    def patch_source_changed(self, pid: str, tile_idx: int) -> None:
        win = self._win
        p = self._project().patch(pid) if self._project() else None
        if p is None:
            return
        p.source_tile_idx = tile_idx
        if p.dst_tile_idx == tile_idx:
            p.dst_tile_idx = -1
        if tile_idx in p.extra_tile_indices:
            p.extra_tile_indices.remove(tile_idx)
        # 已知错误:全取消目标格(dst 清零)后若再点来源格,零尺寸 dst 重算
        # 会把 src 也清零。零尺寸 dst 时跳过重算,保留 src。
        if p.lock_align and p.dst.nw > 0 and p.dst.nh > 0:
            g = self._project().resolve_grid(p.anchor_grid)
            pair = g.align_rect_pair(p.dst, tile_idx)
            if pair is not None:
                p.src, p.dst = pair
        win._frame_view.refresh()
        win._patch_panel.sync_patch_combos(pid)

    def patch_targets_changed(self, pid: str, main_idx: int, extra: list) -> None:
        win = self._win
        p = self._project().patch(pid) if self._project() else None
        if p is None:
            return
        p.dst_tile_idx = main_idx
        p.extra_tile_indices = list(extra)
        if main_idx >= 0:
            g = self._project().resolve_grid(p.anchor_grid)
            if g and g.tiles:
                pair = g.align_from_src_pair(p.src, main_idx)
                if pair is not None:
                    p.dst, p.src = pair
        else:
            # 全取消 = 补丁无效:清空目标几何
            p.dst = Rect(0, 0, 0, 0)
        win._frame_view.refresh()
        win._patch_panel.sync_patch_combos(pid)

    def patch_lock_changed(self, pid: str, locked: bool) -> None:
        win = self._win
        p = self._project().patch(pid) if self._project() else None
        if p is None:
            return
        p.lock_align = locked
        if locked:
            g = self._project().resolve_grid(p.anchor_grid)
            pair = g.align_rect_pair(p.dst, p.source_tile_idx)
            if pair is not None:
                p.src, p.dst = pair
        win._frame_view.refresh()
        win._refresh_scope()

    def patch_deleted(self, pid: str) -> None:
        self._win._state.delete_patch(pid)

    # ---- 复制规则 ----
    def copy_add(self) -> None:
        win = self._win
        if self._project() is None:
            return
        rule = CopyRule(source_tile_idx=0, target_tile_indices=[1])
        win._state.add_copy(rule)
        win._patch_panel.switch_tab("copy")

    def copy_toggled(self, rid: str, on: bool) -> None:
        self._win._state.toggle_copy(rid, on)

    def copy_source_changed(self, rid: str, idx: int) -> None:
        r = self._project().copy_rule(rid) if self._project() else None
        if r is None:
            return
        r.source_tile_idx = idx
        if idx in r.target_tile_indices:
            r.target_tile_indices.remove(idx)
        self._refresh_copy_ui(rid)

    def copy_targets_changed(self, rid: str, indices: list) -> None:
        r = self._project().copy_rule(rid) if self._project() else None
        if r is None:
            return
        r.target_tile_indices = [i for i in indices if i != r.source_tile_idx]
        self._refresh_copy_ui(rid)

    def copy_flip_changed(self, rid: str, on: bool) -> None:
        r = self._project().copy_rule(rid) if self._project() else None
        if r is None:
            return
        r.flip_horizontal = bool(on)
        self._refresh_copy_ui(rid)

    def copy_deleted(self, rid: str) -> None:
        self._win._state.delete_copy(rid)
