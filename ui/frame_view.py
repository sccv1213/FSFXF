"""FrameView:视频画面渲染 + 布局网格 + 补丁矩形绘制/选择/缩放/微调。

三拆(重写):FrameView(画布状态+事件转发)/ FramePainter(纯绘制)/
FrameInteractor(命中+拖拽状态机,`DragState` 类型化 dataclass 替代
旧动态 dict + 字符串 type("rubber"/"pan"/"move"/"resize"/"grid"))。

交互语义(1:1 保留,用户明确要求的细节):
- 复制规则高亮 = 来源/目标格绿/红半透明填充 + 虚线框(保留填充;
  同格混色成黄是半透明叠加的必然,非 bug,不要"修")
- 每个 drawRect 前 setBrush(NoBrush)(手柄 brush 残留曾致整格不透明黄填充)
- 框选起点 clamp 到 [0,1] 再查格(边距区起点也能命中)
- 锚点缩放 1.25 因子 0.1-16;>100% 左键空白拖画布,≤100% 自动居中
- 边缘线 = 最左/最右格子边框,取 project.grid 首/尾(全程统一),离原位变红
- 移动边界守卫:当前 scope 有补丁/复制 → 禁止(弹窗文案在主窗口)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

from core.grid import GridLayout
from core.project import Patch, Project, Rect
from .style import BG, COL_DST, COL_EDGE_IDLE, COL_GRID, COL_SEL, COL_SRC, COL_TEXT

_MIN_NORM = 0.005                   # 最小矩形(归一化)


def _pick_source_tile(grid: GridLayout, dst: Rect, dst_tile: int | None) -> int | None:
    """目标模式自动选来源格:优先选能容纳 dst 宽度的最近格。

    分界线移动后最近格可能比 dst 窄(装不下,align_rect 会收窄造成无谓
    收缩)——此时改选能容纳的最近格;所有格都装不下时退回最近格(创建
    后收窄,总比来源框体越过格界/分界线好)。
    """
    tile = grid.nearest_tile(*dst.center(), exclude=dst_tile)
    if tile is None or grid.tile(tile).nw >= dst.nw:
        return tile
    cx, _ = dst.center()
    fits = [i for i, t in enumerate(grid.tiles)
            if i != dst_tile and t.nw >= dst.nw]
    if fits:
        return min(fits, key=lambda i: abs(grid.tile(i).center()[0] - cx))
    return tile
_HIT_PX = 7                         # 命中容差(屏幕像素)
_PAD = 36                           # 画面四周画布边距(方便从边缘框选)


class DragOp(Enum):
    RUBBER = 0
    PAN = 1
    MOVE_PATCH = 2
    RESIZE_PATCH = 3
    MOVE_EDGE = 4


@dataclass
class DragState:
    """类型化拖拽状态(替代旧动态 dict)。"""
    op: DragOp
    start_pos: QPointF | None = None      # PAN:按下位置
    orig_pan: tuple = (0.0, 0.0)          # PAN
    rubber_start: tuple | None = None     # RUBBER:起点 widget 坐标
    patch: Patch | None = None            # MOVE/RESIZE
    what: str = ""                        # MOVE/RESIZE:"dst"|"src"
    corner: str = ""                      # RESIZE:tl/tr/bl/br
    press_norm: tuple = (0.0, 0.0)        # MOVE:按下时归一化坐标
    orig_dst: Rect | None = None          # MOVE/RESIZE:快照
    orig_src: Rect | None = None
    edge: int = 0                         # MOVE_EDGE:-1 左边缘/-2 右边缘/1..n-1 分界线


class FramePainter:
    """纯绘制函数(无状态)。"""

    @staticmethod
    def paint(fv: "FrameView", p: QPainter) -> None:
        p.fillRect(fv.rect(), BG)
        if fv._frame is not None:
            fv._update_transform()
            dest = QRectF(fv._offx, fv._offy,
                          fv._fw * fv._scale, fv._fh * fv._scale)
            p.drawImage(dest, fv._frame)
            FramePainter._draw_grid(fv, p)
            FramePainter._draw_patches(fv, p)
            FramePainter._draw_copy_highlight(fv, p)   # 复制规则高亮常显
        else:
            p.setPen(QColor(130, 130, 130))
            p.drawText(fv.rect(), Qt.AlignmentFlag.AlignCenter, "打开视频开始（Ctrl+O）")
        # 框选状态在 FrameInteractor(修复:曾读 FrameView._rubber 恒 None 导致不绘制)
        rubber = fv._interactor.rubber
        if rubber is not None:
            x1, y1, x2, y2 = rubber
            r = QRectF(QPointF(x1, y1), QPointF(x2, y2)).normalized()
            fill = QColor(fv._interactor.rubber_color)
            fill.setAlpha(40)
            p.fillRect(r, fill)
            p.setPen(QPen(fv._interactor.rubber_color, 2))
            p.drawRect(r)
        # 状态提示
        p.setPen(COL_TEXT)
        mode_text = {"select": "选择", "target": "目标(红) — 画脏区域",
                     "source": "源(绿) — 画干净区域"}[fv._mode]
        p.drawText(8, fv.height() - 8, f"模式：{mode_text}    缩放 {fv._zoom * 100:.0f}%")

    @staticmethod
    def _draw_grid(fv: "FrameView", p: QPainter) -> None:
        g = fv.current_grid()
        if not g or not g.tiles:
            return
        p.setPen(QPen(COL_GRID, 1, Qt.PenStyle.DashLine))
        f = p.font()
        f.setPointSize(9)
        p.setFont(f)
        for i, t in enumerate(g.tiles):
            x1, y1 = fv._n2w(t.nx, t.ny)
            x2, y2 = fv._n2w(t.nx + t.nw, t.ny + t.nh)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(x1, y1, x2 - x1, y2 - y1))
            p.drawText(QRectF(x1 + 4, y1 + 2, 40, 16),
                       Qt.AlignmentFlag.AlignLeft, f"格{i + 1}")
        # 左右裁剪边缘线(全程统一)= 最左/最右格子边框:原位灰、离开原位红
        _, y_top = fv._n2w(0, 0)
        _, y_bot = fv._n2w(0, 1)
        cl, cr = fv.crop_left(), fv.crop_right()
        left_px = int(round(cl * fv._fw))
        right_px = int(round(cr * fv._fw))
        for edge_x, moved in ((cl, left_px > 0), (cr, right_px < fv._fw)):
            x1, _ = fv._n2w(edge_x, 0)
            color = COL_DST if moved else COL_EDGE_IDLE
            p.setPen(QPen(color, 2))
            p.drawLine(int(x1), int(y_top), int(x1), int(y_bot))

    @staticmethod
    def _draw_patches(fv: "FrameView", p: QPainter) -> None:
        proj = fv._project
        if proj is None:
            return
        # 只画当前 scope 启用的补丁(全局模式 = 全部)
        enabled = fv.enabled_patch_ids()
        for patch in proj.patches:
            if patch.id not in enabled:
                continue
            if patch.dst.nw <= 0 or patch.dst.nh <= 0:
                continue   # 目标格全取消 = 补丁无效(dst 清零),整条不显示
            sel = patch.id == fv._selected
            # 主目标 + 额外目标格派生矩形(带缓存)
            dsts = fv.derived_patch_targets(patch)
            for rect, color in ([*((d, COL_DST) for d in dsts),
                                 (patch.src, COL_SRC)]):
                if rect.nw <= 0 or rect.nh <= 0:
                    continue
                r = fv._rect_to_widget(rect)
                fill = QColor(color)
                fill.setAlpha(36 if not sel else 70)
                p.fillRect(r, fill)
                pen = QPen(QColor(255, 255, 255) if sel else color, 2 if sel else 1.5)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)   # 防手柄 brush 残留
                p.drawRect(r)
                if rect is patch.dst or rect is patch.src:
                    p.drawText(QRectF(r.x() + 2, r.y() - 14, 60, 14),
                               Qt.AlignmentFlag.AlignLeft,
                               "目标" if rect is patch.dst else "源")
            if sel:
                FramePainter._draw_handles(fv, p, patch)

    @staticmethod
    def _draw_copy_highlight(fv: "FrameView", p: QPainter) -> None:
        """复制规则高亮:来源格绿半透明填充 + 虚线框、目标格红半透明填充 + 虚线框。

        用户要求补丁与复制都保留填充;同格时半透明叠加轻微混色属正常。
        (曾出现的"整格不透明黄"是手柄 brush 残留经 drawRect 填充整格所致,
        drawRect 前必须 setBrush(NoBrush)。)
        """
        for item in fv._copy_highlight:
            src_idx, tgt_indices = item[0], item[1]
            flip = bool(item[2]) if len(item) > 2 else False
            if len(item) > 3 and fv._project is not None:
                g = fv._project.resolve_grid(item[3])
            else:
                g = fv.current_grid()
            if not g or not g.tiles:
                continue
            for idx, color in ([(src_idx, COL_SRC)]
                               + [(t, COL_DST) for t in tgt_indices]):
                t = g.tile(idx)
                if t is None:
                    continue
                r = fv._rect_to_widget(t)
                fill = QColor(color)
                fill.setAlpha(40)
                p.fillRect(r, fill)
                p.setPen(QPen(color, 2, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(r)
                if flip and idx in tgt_indices:
                    p.setPen(QColor(255, 255, 255, 220))
                    p.drawText(QRectF(r.x(), r.y() + 2, r.width(), 18),
                               Qt.AlignmentFlag.AlignHCenter, "⇋ 已翻转")

    @staticmethod
    def _draw_handles(fv: "FrameView", p: QPainter, patch: Patch) -> None:
        p.setPen(QPen(COL_SEL, 1.5))
        p.setBrush(COL_SEL)
        for rect in (patch.dst, patch.src if not patch.lock_align else patch.dst):
            for cx, cy in ((rect.nx, rect.ny), (rect.nx + rect.nw, rect.ny),
                           (rect.nx, rect.ny + rect.nh), (rect.nx + rect.nw, rect.ny + rect.nh)):
                wx, wy = fv._n2w(cx, cy)
                p.drawRect(QRectF(wx - 4, wy - 4, 8, 8))
        p.setBrush(Qt.BrushStyle.NoBrush)   # 恢复:防 COL_SEL brush 残留到后续绘制


class FrameInteractor:
    """命中 + 拖拽状态机(编辑语义与旧版 1:1)。"""

    def __init__(self, fv: "FrameView"):
        self.fv = fv
        self.drag: DragState | None = None
        self.rubber: tuple | None = None
        self.rubber_tile: Rect | None = None
        self.rubber_color = COL_DST
        self.rubber_mode = "target"   # 本次框选模式(target/source),release 时消费

    # ---- 命中 ----
    def tile_at_pos(self, pos: QPointF) -> Rect | None:
        """鼠标位置所在格;不在任何格内返回 None(坐标先 clamp 到画面内再查格)。"""
        g = self.fv.current_grid()
        if not g or not g.tiles:
            return None
        nx, ny = self.fv._w2n(pos)
        nx, ny = self._clamp_norm(nx, ny, None)
        idx = g.tile_index_at(nx, ny)
        return g.tiles[idx] if idx is not None else None

    @staticmethod
    def _clamp_norm(nx: float, ny: float, tile: Rect | None) -> tuple[float, float]:
        if tile is None:
            return max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))
        return (max(tile.nx, min(tile.nx + tile.nw, nx)),
                max(tile.ny, min(tile.ny + tile.nh, ny)))

    def hit_grid_edge(self, pos: QPointF) -> int | None:
        """命中的边界:1..len-1 内部分界线;-1 = 左边缘线;-2 = 右边缘线。"""
        fv = self.fv
        g = fv.current_grid()
        if g is None or not g.tiles:
            return None
        for i in range(1, len(g.tiles)):
            wx, _ = fv._n2w(g.tiles[i].nx, 0)
            if abs(wx - pos.x()) <= _HIT_PX:
                return i
        wl, _ = fv._n2w(fv.crop_left(), 0)
        if abs(wl - pos.x()) <= _HIT_PX:
            return -1
        wr, _ = fv._n2w(fv.crop_right(), 0)
        if abs(wr - pos.x()) <= _HIT_PX:
            return -2
        return None

    def hit_handle(self, patch: Patch, pos: QPointF) -> tuple[str, str] | None:
        """返回 (rect标识, 角) 或 None。"""
        targets = [("dst", patch.dst)]
        if not patch.lock_align:
            targets.append(("src", patch.src))
        for what, r in targets:
            for corner in ("tl", "tr", "bl", "br"):
                if corner == "tl":
                    cx, cy = r.nx, r.ny
                elif corner == "tr":
                    cx, cy = r.nx + r.nw, r.ny
                elif corner == "bl":
                    cx, cy = r.nx, r.ny + r.nh
                else:
                    cx, cy = r.nx + r.nw, r.ny + r.nh
                wx, wy = self.fv._n2w(cx, cy)
                if abs(wx - pos.x()) <= _HIT_PX and abs(wy - pos.y()) <= _HIT_PX:
                    return (what, corner)
        return None

    def hit_rect(self, patch: Patch, pos: QPointF) -> str | None:
        for what, r in (("dst", patch.dst), ("src", patch.src)):
            if r.nw <= 0:
                continue
            if self.fv._rect_to_widget(r).contains(pos):
                return what
        return None

    def hit_select(self, pos: QPointF) -> DragState | None:
        """选择模式命中:手柄 → RESIZE_PATCH;矩形内 → MOVE_PATCH。"""
        fv = self.fv
        if fv._project is None:
            return None
        enabled = fv.enabled_patch_ids()
        for patch in reversed(fv._project.patches):
            if patch.id not in enabled:
                continue
            if patch.dst.nw <= 0 or patch.dst.nh <= 0:
                continue   # 无效补丁(目标格全取消)不可点选
            hit = self.hit_handle(patch, pos)
            if hit:
                return DragState(DragOp.RESIZE_PATCH, patch=patch, what=hit[0],
                                 corner=hit[1],
                                 orig_dst=Rect(patch.dst.nx, patch.dst.ny,
                                               patch.dst.nw, patch.dst.nh),
                                 orig_src=Rect(patch.src.nx, patch.src.ny,
                                               patch.src.nw, patch.src.nh))
            what = self.hit_rect(patch, pos)
            if what:
                return DragState(DragOp.MOVE_PATCH, patch=patch, what=what,
                                 press_norm=fv._w2n(pos),
                                 orig_dst=Rect(patch.dst.nx, patch.dst.ny,
                                               patch.dst.nw, patch.dst.nh),
                                 orig_src=Rect(patch.src.nx, patch.src.ny,
                                               patch.src.nw, patch.src.nh))
        return None

    # ---- 鼠标 ----
    def on_press(self, e) -> None:
        fv = self.fv
        if e.button() == Qt.MouseButton.LeftButton:
            if fv._mode in (fv.MODE_TARGET, fv.MODE_SOURCE):
                pos = e.position()
                self.rubber_tile = self.tile_at_pos(pos)
                self.rubber = (pos.x(), pos.y(), pos.x(), pos.y())
                self.rubber_color = COL_DST if fv._mode == fv.MODE_TARGET else COL_SRC
                self.rubber_mode = fv._mode
                self.drag = DragState(DragOp.RUBBER, rubber_start=(pos.x(), pos.y()))
            else:
                # 选择模式 = 网格+补丁整合:悬停分界/边缘线 → 移线;否则补丁/空白
                edge = self.hit_grid_edge(e.position())
                if edge is not None:
                    if fv.edge_move_blocked():
                        fv.edgeMoveBlocked.emit()
                    else:
                        self.drag = DragState(DragOp.MOVE_EDGE, edge=edge)
                else:
                    drag = self.hit_select(e.position())
                    self.drag = drag
                    if drag is None:
                        if fv._zoom > 1.0:
                            self.drag = DragState(DragOp.PAN, start_pos=e.position(),
                                                  orig_pan=(fv._pan_x, fv._pan_y))
                            fv.setCursor(Qt.CursorShape.ClosedHandCursor)
                        else:
                            fv._select(None)
                    else:
                        fv._select(drag.patch.id)
                        fv.setCursor(Qt.CursorShape.ClosedHandCursor)

    def on_move(self, e) -> None:
        fv = self.fv
        # 选择模式下跟踪悬停的分界线(方向键移线用)
        fv._hover_grid_edge = (self.hit_grid_edge(e.position())
                               if fv._mode == fv.MODE_SELECT else None)
        nx, ny = fv._w2n(e.position())
        fv.mousePosChanged.emit(nx, ny)
        d = self.drag
        if d is None:
            return
        if d.op is DragOp.RUBBER:
            nx, ny = fv._w2n(e.position())
            nx, ny = self._clamp_norm(nx, ny, self.rubber_tile)
            wx, wy = fv._n2w(nx, ny)
            x, y = d.rubber_start
            self.rubber = (x, y, wx, wy)
            fv.update()
        elif d.op is DragOp.PAN:
            dx = e.position().x() - d.start_pos.x()
            dy = e.position().y() - d.start_pos.y()
            fv._pan_x = d.orig_pan[0] + dx / fv._scale   # 视频像素偏移
            fv._pan_y = d.orig_pan[1] + dy / fv._scale
            fv._update_transform()
            fv.update()
        elif d.op is DragOp.MOVE_PATCH:
            self._apply_move(d, e.position())
        elif d.op is DragOp.RESIZE_PATCH:
            self._apply_resize(d, e.position())
        elif d.op is DragOp.MOVE_EDGE:
            self._apply_grid_edge(d, e.position())

    def on_release(self, e) -> None:
        fv = self.fv
        d = self.drag
        self.drag = None
        if d is None:
            return
        if d.op is DragOp.RUBBER and self.rubber is not None:
            x1, y1, x2, y2 = self.rubber
            self.rubber = None
            self._finish_rubber(x1, y1, x2, y2)
            self.rubber_tile = None
        elif d.op in (DragOp.MOVE_PATCH, DragOp.RESIZE_PATCH):
            fv.patchChanged.emit(d.patch)
        fv._update_cursor(e.position())

    # ---- 操作应用 ----
    @staticmethod
    def _clamp_rect(r: Rect) -> Rect:
        return Rect(max(0.0, min(1.0 - _MIN_NORM, r.nx)),
                    max(0.0, min(1.0 - _MIN_NORM, r.ny)),
                    max(_MIN_NORM, min(1.0 - r.nx, r.nw)),
                    max(_MIN_NORM, min(1.0 - r.ny, r.nh)))

    def _constrain_to_orig_tile(self, rect: Rect, orig: Rect, g) -> Rect:
        """把矩形限制在 orig 所在格内;四方向越界都"挤压",行为对称。

        g = 补丁所属网格(patch_grid)——修复:曾用当前 scope 网格,补丁锚定
        project 而段网格物化后两者脱节,格子线移动后约束错位。
        """
        tile = None
        if g and g.tiles:
            idx = g.tile_index_at(*orig.center())
            if idx is not None:
                tile = g.tiles[idx]
        if tile is None:
            return self._clamp_rect(rect)
        left = max(tile.nx, rect.nx)
        right = min(tile.nx + tile.nw, rect.nx + rect.nw)
        top = max(tile.ny, rect.ny)
        bottom = min(tile.ny + tile.nh, rect.ny + rect.nh)
        return Rect(left, top,
                    max(_MIN_NORM, right - left),
                    max(_MIN_NORM, bottom - top))

    def _apply_move(self, d: DragState, pos: QPointF) -> None:
        fv = self.fv
        nx, ny = fv._w2n(pos)
        dx = nx - d.press_norm[0]
        dy = ny - d.press_norm[1]
        p = d.patch
        g = fv.patch_grid(p)
        if d.what == "dst":
            p.dst = self._constrain_to_orig_tile(
                Rect(d.orig_dst.nx + dx, d.orig_dst.ny + dy,
                     d.orig_dst.nw, d.orig_dst.nh), d.orig_dst, g)
            if p.lock_align:
                self._realign(p)
        else:
            p.src = self._constrain_to_orig_tile(
                Rect(d.orig_src.nx + dx, d.orig_src.ny + dy,
                     d.orig_src.nw, d.orig_src.nh), d.orig_src, g)
        self._sync_tile_indices(p)
        fv.update()

    def _apply_resize(self, d: DragState, pos: QPointF) -> None:
        fv = self.fv
        nx, ny = fv._w2n(pos)
        p = d.patch
        orig = d.orig_dst if d.what == "dst" else d.orig_src
        left, right = orig.nx, orig.nx + orig.nw
        top, bottom = orig.ny, orig.ny + orig.nh
        if "l" in d.corner:
            left = nx
        if "r" in d.corner:
            right = nx
        if "t" in d.corner:
            top = ny
        if "b" in d.corner:
            bottom = ny
        new = Rect(min(left, right), min(top, bottom),
                   abs(right - left), abs(bottom - top))
        new = self._clamp_rect(new)
        g = fv.patch_grid(p)
        if d.what == "dst":
            p.dst = self._constrain_to_orig_tile(new, d.orig_dst, g)
            if p.lock_align:
                self._realign(p)
        else:
            p.src = self._constrain_to_orig_tile(new, d.orig_src, g)
        self._sync_tile_indices(p)
        fv.update()

    def _sync_tile_indices(self, p: Patch) -> None:
        """矩形被手动编辑后,把源/目标所在格索引同步回模型。"""
        g = self.fv.patch_grid(p)
        if not g or not g.tiles:
            return
        idx = g.tile_index_at(*p.dst.center())
        p.dst_tile_idx = idx if idx is not None else -1
        idx = g.tile_index_at(*p.src.center())
        p.source_tile_idx = idx if idx is not None else -1

    def _realign(self, p: Patch) -> None:
        g = self.fv.patch_grid(p)
        if g and g.tiles and p.source_tile_idx >= 0:
            pair = g.align_rect_pair(p.dst, p.source_tile_idx)
            if pair is not None:
                p.src, p.dst = pair   # (src, dst) 同尺寸对(含格内收窄)

    def _apply_grid_edge(self, d: DragState, pos: QPointF) -> None:
        fv = self.fv
        g = fv.current_grid()
        if g is None or d.edge == 0:
            return
        if fv.edge_move_blocked():
            fv.edgeMoveBlocked.emit()
            return
        nx, _ = fv._w2n(pos)
        edge = d.edge
        if edge == -1:
            fv.move_crop_left(nx)
        elif edge == -2:
            fv.move_crop_right(nx)
        else:
            fv.move_inner_edge(edge, nx)
        fv.update()

    def nudge_grid_edge(self, key) -> None:
        """方向键微调悬停的分界线/边缘线:一次一个视频像素。"""
        fv = self.fv
        g = fv.current_grid()
        if g is None or fv._hover_grid_edge is None:
            return
        if fv.edge_move_blocked():
            fv.edgeMoveBlocked.emit()
            return
        edge = fv._hover_grid_edge
        dx = 1.0 / fv._fw if key == Qt.Key.Key_Right else -1.0 / fv._fw
        if edge == -1:
            fv.move_crop_left(fv.crop_left() + dx)
        elif edge == -2:
            fv.move_crop_right(fv.crop_right() + dx)
        else:
            fv.move_inner_edge(edge, g.tiles[edge].nx + dx)
        fv.update()

    def nudge(self, key, px: int) -> None:
        fv = self.fv
        if not fv._selected:
            return
        p = fv._project.patch(fv._selected) if fv._project else None
        if p is None:
            return
        dx = dy = 0.0
        if key == Qt.Key.Key_Left:
            dx = -px
        elif key == Qt.Key.Key_Right:
            dx = px
        elif key == Qt.Key.Key_Up:
            dy = -px
        elif key == Qt.Key.Key_Down:
            dy = px
        new_dst = Rect(p.dst.nx + dx / fv._fw, p.dst.ny + dy / fv._fh,
                       p.dst.nw, p.dst.nh)
        p.dst = self._constrain_to_orig_tile(new_dst, p.dst, fv.patch_grid(p))   # 限制在所属格内
        if p.lock_align:
            self._realign(p)
        self._sync_tile_indices(p)
        fv.patchChanged.emit(p)
        fv.update()

    def _finish_rubber(self, x1, y1, x2, y2) -> None:
        fv = self.fv
        n1 = self._clamp_norm(*fv._w2n(QPointF(x1, y1)), self.rubber_tile)
        n2 = self._clamp_norm(*fv._w2n(QPointF(x2, y2)), self.rubber_tile)
        nx, ny = min(n1[0], n2[0]), min(n1[1], n2[1])
        nw, nh = abs(n1[0] - n2[0]), abs(n1[1] - n2[1])
        if nw < _MIN_NORM or nh < _MIN_NORM:
            fv.update()
            return
        drawn = Rect(nx, ny, nw, nh)
        grid = fv.current_grid()
        has_grid = bool(grid and grid.tiles)

        if self.rubber_mode == fv.MODE_SOURCE:
            # 源模式:画的矩形 = 干净源区域;目标格 = 相邻格(左优先),dst 自动映射
            src = drawn
            dst = None
            src_tile = grid.tile_index_at(*src.center()) if has_grid else None
            dst_tile = None
            if has_grid:
                if src_tile is not None and src_tile > 0:
                    dst_tile = src_tile - 1
                elif src_tile is not None and len(grid.tiles) > 1:
                    dst_tile = src_tile + 1
                elif src_tile is not None:
                    dst_tile = src_tile
                pair = grid.align_from_src_pair(src, dst_tile)
                if pair is not None:
                    dst, src = pair   # (dst, src) 同尺寸对(含格内收窄)
            if dst is None:
                dst = Rect(drawn.nx, drawn.ny, drawn.nw, drawn.nh)   # 映射失败:独立副本
            p = Patch(dst=dst, src=src,
                      source_tile_idx=src_tile if src_tile is not None else -1,
                      dst_tile_idx=dst_tile if dst_tile is not None else -1,
                      lock_align=bool(has_grid and dst_tile is not None),
                      anchor_grid=fv.scope_anchor())
        else:
            # 目标模式:画的矩形 = 脏区域;来源格 = 自动(优先能容纳 dst 的格),
            # src 自动映射;来源格装不下时同步收窄 dst(格内约束,不出分界线)
            dst = drawn
            dst_tile = grid.tile_index_at(*dst.center()) if has_grid else None
            src_tile = None
            if has_grid:
                src_tile = _pick_source_tile(grid, dst, dst_tile)
                pair = grid.align_rect_pair(dst, src_tile)
                if pair is not None:
                    src, dst = pair
                else:
                    src = Rect(nx, ny, nw, nh)
            else:
                src = Rect(nx, ny, nw, nh)
            p = Patch(dst=dst, src=src,
                      source_tile_idx=src_tile if src_tile is not None else -1,
                      dst_tile_idx=dst_tile if dst_tile is not None else -1,
                      lock_align=bool(has_grid and src_tile is not None),
                      anchor_grid=fv.scope_anchor())
        fv.patchCreated.emit(p)
        fv.update()


class FrameView(QWidget):
    """画面画布:持画布状态,绘制委托 FramePainter,交互委托 FrameInteractor。"""

    patchCreated = Signal(object)        # Patch(用户画完目标矩形)
    patchChanged = Signal(object)        # Patch(位置/尺寸被编辑)
    mousePosChanged = Signal(float, float)  # 归一化坐标(状态栏)
    edgeMoveBlocked = Signal()           # 当前段有补丁/复制时尝试移动边界(主窗口提示)

    MODE_SELECT = "select"
    MODE_TARGET = "target"
    MODE_SOURCE = "source"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(480, 320)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._frame: QImage | None = None
        self._last_frame_pts = -1.0
        self._fw = 1920
        self._fh = 1080
        self._scale = 1.0
        self._offx = self._offy = 0.0
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._mode = self.MODE_SELECT
        self._project: Project | None = None
        self._selected: str | None = None
        self._cur_grid: GridLayout | None = None
        self._copy_highlight: list[tuple[int, list[int]]] = []
        self._derived_dsts_cache: dict[tuple, list[Rect]] = {}
        self._hover_grid_edge: int | None = None
        self._scope_anchor = "project"     # 当前 scope 的 grid_key(补丁创建锚定用)
        self._enabled_ids: set[str] = set()   # 当前 scope 启用的补丁 id(绘制/命中)
        self._enabled_copy_ids: set[str] = set()   # 当前 scope 启用的复制规则(守卫)
        self._state = None                 # AppState(网格编辑路径)
        self._interactor = FrameInteractor(self)

    # ---------- 外部设置 ----------
    def set_frame(self, img: QImage | None, pts: float = 0.0) -> None:
        if img is not None:
            self._frame = img
            self._fw = max(1, img.width())
            self._fh = max(1, img.height())
            self._last_frame_pts = pts
            self.update()
        elif self._last_frame_pts != pts:
            self._last_frame_pts = pts
            self.update()   # 仅帧 pts 变化才重绘(播放中同一 pts 不重绘)

    def set_project(self, project: Project | None) -> None:
        self._project = project
        self._selected = None
        self._derived_dsts_cache.clear()
        self.update()

    def set_scope_context(self, scope) -> None:
        """推送当前 scope(主窗口在 scope_changed 时调用):网格/启用集合/锚定。"""
        if scope is None:
            self._cur_grid = None
            self._enabled_ids = set()
            self._enabled_copy_ids = set()
            self._scope_anchor = "project"
        else:
            self._cur_grid = scope.grid
            self._scope_anchor = scope.grid_key
            self._enabled_ids = set(scope.patch_ids)
            self._enabled_copy_ids = set(scope.copy_rule_ids)
        self._derived_dsts_cache.clear()
        self.update()

    def set_copy_highlight(self, rules: list[tuple[int, list[int]]]) -> None:
        """复制模式高亮:[(来源格, [目标格...]), ...](当前段启用的规则)。"""
        self._copy_highlight = list(rules)
        self._derived_dsts_cache.clear()
        self.update()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._interactor.drag = None
        self._interactor.rubber = None
        self.update()

    def select_patch(self, patch_id: str | None) -> None:
        self._select(patch_id)

    def enabled_patch_ids(self) -> set[str]:
        """当前 scope 启用的补丁 id(与绘制/命中一致的唯一来源)。"""
        return self._enabled_ids

    def scope_anchor(self) -> str:
        return self._scope_anchor

    def current_grid(self) -> GridLayout | None:
        if self._cur_grid is not None:
            return self._cur_grid
        return self._project.grid if self._project else None

    def derived_patch_targets(self, patch: Patch) -> list[Rect]:
        """主目标 + 额外目标格派生矩形(带缓存;refresh/scope 变化时清空)。"""
        key_rect = (round(patch.src.nx, 9), round(patch.src.ny, 9),
                    round(patch.src.nw, 9), round(patch.src.nh, 9))
        g = self.patch_grid(patch)
        key = (patch.id, patch.anchor_grid, key_rect,
               tuple(patch.extra_tile_indices),
               tuple((round(t.nx, 9), round(t.ny, 9), round(t.nw, 9), round(t.nh, 9))
                     for t in g.tiles) if g is not None and g.tiles else None)
        if key not in self._derived_dsts_cache:
            dsts = [patch.dst]
            if g is not None and g.tiles:
                for ti in patch.extra_tile_indices:
                    d = g.align_from_src(patch.src, ti)
                    if d is not None:
                        dsts.append(d)
            self._derived_dsts_cache[key] = dsts
        return self._derived_dsts_cache[key]

    def patch_grid(self, p: Patch) -> GridLayout | None:
        """补丁所属网格:统一经 Project.resolve_grid(全项目唯一解析路径)。"""
        if self._project is None:
            return None
        return self._project.resolve_grid(p.anchor_grid)

    def crop_left(self) -> float:
        """全局左边缘线位置(全程统一存 project.grid 首边)。"""
        if self._project is not None:
            return self._project.grid.crop_left
        g = self.current_grid()
        return g.crop_left if g else 0.0

    def crop_right(self) -> float:
        if self._project is not None:
            return self._project.grid.crop_right
        g = self.current_grid()
        return g.crop_right if g else 1.0

    def edge_move_blocked(self) -> bool:
        """当前 scope 启用补丁/复制规则 → 禁止移动边界(防对齐错位)。"""
        return bool(self._enabled_ids) or bool(self._enabled_copy_ids)

    # ---- 编辑动作(直接委托 AppState:ui → state 依赖方向) ----
    def move_crop_left(self, lo: float) -> None:
        if self._state is not None:
            self._state.set_crop(lo, self.crop_right())

    def move_crop_right(self, hi: float) -> None:
        if self._state is not None:
            self._state.set_crop(self.crop_left(), hi)

    def move_inner_edge(self, edge_idx: int, nx: float) -> None:
        if self._state is not None:
            self._state.move_inner_edge(edge_idx, nx)

    def set_state(self, state) -> None:
        """注入 AppState(网格编辑路径的单一数据源)。"""
        self._state = state

    def refresh(self) -> None:
        self._derived_dsts_cache.clear()
        self.update()

    # ---------- 坐标变换 ----------
    def _base_transform(self) -> tuple[float, float, float]:
        dw = max(10, self.width() - 2 * _PAD)
        dh = max(10, self.height() - 2 * _PAD)
        base = min(dw / self._fw, dh / self._fh)
        scale = base * self._zoom
        cx = _PAD + (dw - self._fw * scale) / 2
        cy = _PAD + (dh - self._fh * scale) / 2
        return scale, cx, cy

    def _update_transform(self) -> None:
        if self._frame is None:
            return
        scale, cx, cy = self._base_transform()
        self._scale = scale
        if self._zoom > 1.0:
            self._offx = cx + self._pan_x * scale
            self._offy = cy + self._pan_y * scale
        else:
            self._pan_x = 0.0
            self._pan_y = 0.0
            self._offx = cx
            self._offy = cy

    def _w2n(self, pos: QPointF) -> tuple[float, float]:
        if self._scale <= 0:
            return (0.0, 0.0)
        return ((pos.x() - self._offx) / self._scale / self._fw,
                (pos.y() - self._offy) / self._scale / self._fh)

    def _n2w(self, nx: float, ny: float) -> tuple[float, float]:
        return (self._offx + nx * self._fw * self._scale,
                self._offy + ny * self._fh * self._scale)

    def _rect_to_widget(self, r: Rect) -> QRectF:
        x1, y1 = self._n2w(r.nx, r.ny)
        x2, y2 = self._n2w(r.nx + r.nw, r.ny + r.nh)
        return QRectF(x1, y1, x2 - x1, y2 - y1)

    # ---------- 事件 ----------
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        try:
            FramePainter.paint(self, p)
        finally:
            p.end()

    def mousePressEvent(self, e) -> None:
        if self._frame is None:
            return
        self.setFocus()
        self._interactor.on_press(e)
        e.accept()

    def mouseMoveEvent(self, e) -> None:
        self._interactor.on_move(e)
        if self._interactor.drag is None:
            self._update_cursor(e.position())
        e.accept()

    def mouseReleaseEvent(self, e) -> None:
        self._interactor.on_release(e)
        e.accept()

    def enterEvent(self, e) -> None:
        # 悬停上报:方向键分发依赖 hover_target(旧版 widgetAt 实时查,新版事件维护)
        if self._state is not None:
            self._state.set_hover("frame")
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        # 离开画面:上报 window + 清分界线悬停(mouseLeaveEvent 不是 Qt 虚方法)
        self._hover_grid_edge = None
        if self._state is not None:
            self._state.set_hover("window")
        super().leaveEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._zoom = 1.0
            self.update()

    def wheelEvent(self, e) -> None:
        if self._frame is None:
            return
        factor = 1.25 ** (e.angleDelta().y() / 120.0)
        new_zoom = max(0.1, min(16.0, self._zoom * factor))
        if new_zoom == self._zoom:
            return
        self._update_transform()
        nx, ny = self._w2n(e.position())   # 缩放前光标处的视频坐标
        self._zoom = new_zoom
        scale, cx, cy = self._base_transform()
        if new_zoom > 1.0:
            # 锚点:缩放后光标处视频坐标仍为 (nx,ny) → 调整平移偏移
            self._pan_x = (e.position().x() - nx * self._fw * scale - cx) / scale
            self._pan_y = (e.position().y() - ny * self._fh * scale - cy) / scale
        else:
            self._pan_x = 0.0
            self._pan_y = 0.0
        self._update_transform()
        self.update()

    def nudge_arrows_active(self) -> bool:
        """方向键微调激活态:选中补丁或悬停分界/边缘线(键路由查询)。"""
        return (self._mode == self.MODE_SELECT
                and (self._selected or self._hover_grid_edge is not None))

    def event(self, e) -> bool:
        # 焦点在画面时的 ShortcutOverride 拦截(防全局秒跳 QShortcut 抢键):
        # 应用级 KeyRouter 只按悬停目标分发,焦点级抢键必须在此
        if (e.type() == QEvent.Type.ShortcutOverride
                and e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right)
                and self.nudge_arrows_active()):
            e.accept()
            return True
        return super().event(e)

    def keyPressEvent(self, e) -> None:
        # 焦点在画面时的按键入口(与 KeyRouter 共用 handle_key,未消费上抛兜底)
        if not self.handle_key(e.key(), e.modifiers()):
            super().keyPressEvent(e)

    def handle_key(self, key, modifiers) -> bool:
        """键路由直接调用的按键处理(替代旧 keyPressEvent 魔法与 sendEvent 重入)。

        返回 True = 已消费。Escape 取消 / 方向键微调。
        删除补丁只走右栏"删除"按钮(用户要求,不再用 Delete 键)。
        """
        if key == Qt.Key.Key_Escape:
            self._interactor.drag = None
            self._interactor.rubber = None
            self.update()
            return True
        if key == Qt.Key.Key_Left or key == Qt.Key.Key_Right:
            if self._mode == self.MODE_SELECT and self._hover_grid_edge is not None:
                self._interactor.nudge_grid_edge(key)   # 悬停分界/边缘线:移线
                return True
            if self._mode == self.MODE_SELECT and self._selected:
                n = 10 if (modifiers & Qt.KeyboardModifier.ShiftModifier) else 1
                self._interactor.nudge(key, n)          # 选中补丁:微调矩形
                return True
            return False   # 未消费:主窗口秒跳
        if key == Qt.Key.Key_Up or key == Qt.Key.Key_Down:
            if self._mode == self.MODE_SELECT and self._selected:
                n = 10 if (modifiers & Qt.KeyboardModifier.ShiftModifier) else 1
                self._interactor.nudge(key, n)
                return True
            return False
        return False

    # ---------- 其他 ----------
    def _select(self, patch_id: str | None) -> None:
        """画布内选中状态(绘制高亮/方向键微调用,不涉及外部回显)。"""
        self._selected = patch_id
        self.update()

    def _update_cursor(self, pos: QPointF) -> None:
        if self._frame is None:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if self._mode == self.MODE_TARGET:
            self.setCursor(Qt.CursorShape.CrossCursor)
            return
        if self._mode == self.MODE_SELECT:
            # 选择模式 = 网格+补丁整合:悬停分界/边缘线 → 左右箭头;补丁手柄 → 斜向箭头
            if self._interactor.hit_grid_edge(pos) is not None:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return
            if self._project:
                for patch in reversed(self._project.patches):
                    if self._interactor.hit_handle(patch, pos):
                        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
                        return
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.update()
