"""布局网格:竖条格子列表(值语义)。

设计说明(重写时的两个修正,记录于 CLAUDE.md):
- 曾计划把网格改存"分界线列表 edges",但旧布局允许格子间带间隙(带黑边的
  拼屏布局,格0 [0,0.3] 与格1 [0.34,0.64] 之间的 0.04 不属于任何格),
  edges 连续表示会把间隙当成格 → 放弃,保持 tiles 列表值语义。
- move_edge 重构为"先算后写":旧版"先保存 nxt 旧右边界"的顺序修复
  (改 nxt.nx 前先读 right)因"边写边读"而出错;新版所有新值先计算,
  再一次性写回——相邻分界线随动在结构上不可能。

对齐映射 align_rect/align_from_src:相对坐标映射 + 尺寸锁定(src/dst 同
像素尺寸),映射结果超出格子边界(分界线)时收窄贴格右(格内约束,
调用方同步收窄另一矩形保持同尺寸)。
"""
from __future__ import annotations

from .rect import Rect

MIN_TILE = 0.02    # 最小格宽 2%(全部 clamp 的唯一归宿)


class GridLayout:
    """竖条网格。tiles 为唯一数据(格矩形,可带间隙);克隆用 copy()。"""

    def __init__(self, tiles: list[Rect] | None = None):
        self.tiles: list[Rect] = list(tiles) if tiles else []

    # ---- 裁剪边缘线(= 最左/最右格子边界,派生) ----
    @property
    def crop_left(self) -> float:
        return self.tiles[0].nx if self.tiles else 0.0

    @property
    def crop_right(self) -> float:
        return self.tiles[-1].nx + self.tiles[-1].nw if self.tiles else 1.0

    # ---- 预设 ----
    @staticmethod
    def thirds() -> "GridLayout":
        """三格 9:16 竖屏:三等分。"""
        return GridLayout([Rect(0, 0, 1 / 3, 1), Rect(1 / 3, 0, 1 / 3, 1), Rect(2 / 3, 0, 1 / 3, 1)])

    @staticmethod
    def halves() -> "GridLayout":
        """两格 8:9:左右各半。"""
        return GridLayout([Rect(0, 0, 0.5, 1), Rect(0.5, 0, 0.5, 1)])

    @staticmethod
    def quarters() -> "GridLayout":
        """四格:四等分竖条。"""
        return GridLayout([Rect(i / 4, 0, 1 / 4, 1) for i in range(4)])

    # ---- 查询 ----
    def tile_index_at(self, nx: float, ny: float) -> int | None:
        for i, t in enumerate(self.tiles):
            if t.contains(nx, ny):
                return i
        return None

    def tile(self, idx: int) -> Rect | None:
        return self.tiles[idx] if 0 <= idx < len(self.tiles) else None

    def nearest_tile(self, nx: float, ny: float, exclude: int | None = None) -> int | None:
        """找包含该点的格;不在任何格内时找中心最近的一格(排除 exclude)。"""
        idx = self.tile_index_at(nx, ny)
        if idx is not None and idx != exclude:
            return idx
        best, best_d = None, float("inf")
        for i, t in enumerate(self.tiles):
            if i == exclude:
                continue
            cx, cy = t.center()
            d = (cx - nx) ** 2 + (cy - ny) ** 2
            if d < best_d:
                best, best_d = i, d
        return best

    # ---- 对齐映射 ----
    def align_rect(self, dst: Rect, src_tile_idx: int | None) -> Rect | None:
        """把目标矩形映射到来源格子的"同一相对坐标",尺寸保持与 dst 一致。

        位置 = 来源格子内与 dst 相同的相对坐标;宽高 = dst 的宽高(不缩放)——
        补丁从干净格裁像素盖到脏格,src/dst 必须同像素尺寸(渲染端强制),
        格子不必等宽。dst 不在任何格子内时返回 None(UI 层提示拆开)。

        格内约束:来源格比 dst 窄(分界线移动后)时,映射结果会越过格界
        (分界线)——收窄 src 贴格右(相对位置不变);调用方必须同步收窄
        dst 保持同尺寸(创建/重算路径),渲染端 min(sw,dw) 是最终兜底。
        """
        if not self.tiles or src_tile_idx is None:
            return None
        if dst.nw <= 0 or dst.nh <= 0:
            return None   # 零尺寸(dst 清零=补丁停用)不做映射,防 src 被清零
        dst_idx = self.tile_index_at(*dst.center())
        if dst_idx is None:
            return None
        t_dst = self.tiles[dst_idx]
        t_src = self.tile(src_tile_idx)
        if t_src is None:
            return None

        def rel(v: float) -> float:
            return max(0.0, min(1.0, v))

        rx, ry = rel((dst.nx - t_dst.nx) / t_dst.nw), rel((dst.ny - t_dst.ny) / t_dst.nh)
        # 尺寸锁定:src 与 dst 同像素尺寸(移分界线后格宽不等时不再按宽度比缩放)
        src = Rect(t_src.nx + rx * t_src.nw, t_src.ny + ry * t_src.nh,
                   dst.nw, dst.nh)
        if src.nx + src.nw > t_src.nx + t_src.nw:
            src.nw = t_src.nx + t_src.nw - src.nx
        if src.ny + src.nh > t_src.ny + t_src.nh:
            src.nh = t_src.ny + t_src.nh - src.ny
        return src

    def align_from_src(self, src: Rect, dst_tile_idx: int | None) -> Rect | None:
        """对称映射(源模式):src 在其格内相对坐标 → 目标格同相对坐标,尺寸保持。

        格内约束与 align_rect 对称:目标格比 src 窄时收窄 dst 贴格右,
        调用方同步收窄 src 保持同尺寸。
        """
        if not self.tiles or dst_tile_idx is None:
            return None
        if src.nw <= 0 or src.nh <= 0:
            return None   # 零尺寸不做映射,防 dst 被清零
        src_idx = self.tile_index_at(*src.center())
        if src_idx is None:
            return None
        t_src = self.tiles[src_idx]
        t_dst = self.tile(dst_tile_idx)
        if t_dst is None:
            return None

        def rel(v: float) -> float:
            return max(0.0, min(1.0, v))

        rx, ry = rel((src.nx - t_src.nx) / t_src.nw), rel((src.ny - t_src.ny) / t_src.nh)
        # 尺寸锁定:对称于 align_rect,保持 src 尺寸
        dst = Rect(t_dst.nx + rx * t_dst.nw, t_dst.ny + ry * t_dst.nh,
                   src.nw, src.nh)
        if dst.nx + dst.nw > t_dst.nx + t_dst.nw:
            dst.nw = t_dst.nx + t_dst.nw - dst.nx
        if dst.ny + dst.nh > t_dst.ny + t_dst.nh:
            dst.nh = t_dst.ny + t_dst.nh - dst.ny
        return dst

    def align_rect_pair(self, dst: Rect, src_tile_idx: int | None) -> tuple | None:
        """align_rect + 同步收窄 dst:返回 (src, dst) 同尺寸对。

        格内约束要求 src/dst 同尺寸且各自在格内——单边收窄后调用方必须
        同步另一矩形;本函数把"映射 + 收窄 + 同步"收敛一处,杜绝遗漏。
        """
        src = self.align_rect(dst, src_tile_idx)
        if src is None:
            return None
        return src, Rect(dst.nx, dst.ny, src.nw, src.nh)

    def align_from_src_pair(self, src: Rect, dst_tile_idx: int | None) -> tuple | None:
        """align_from_src + 同步收窄 src:返回 (dst, src) 同尺寸对。"""
        dst = self.align_from_src(src, dst_tile_idx)
        if dst is None:
            return None
        return dst, Rect(src.nx, src.ny, dst.nw, dst.nh)

    # ---- 编辑(唯一写入口) ----
    def move_edge(self, i: int, v: float) -> None:
        """把第 i 条竖边界移到 v(归一化)。只移动这一条,其他分界线不动。

        i:0 = 最左边界(左边缘线,范围 [0, 0.5])、len(tiles) = 最右边界
        (右边缘线,范围 [0.5, 1])、1..len-1 = 内部分界线。
        限制:不超出视频边界 [0,1],格子最小宽 2%。

        先算后写:所有新字段值先计算,再一次性写回——"改一个 Rect 时
        读到另一个 Rect 的旧值"的顺序 bug(相邻分界线随动)结构性不可能。
        """
        n = len(self.tiles)
        if n == 0:
            return
        if i == 0:
            t = self.tiles[0]
            right = t.nx + t.nw          # 读旧值(此时未修改)
            hi = min(0.5, right - MIN_TILE)
            if hi <= 0.0:
                return
            t.nx = max(0.0, min(hi, v))
            t.nw = right - t.nx
            return
        if i == n:
            t = self.tiles[-1]
            lo = max(0.5, t.nx + MIN_TILE)
            if lo > 1.0:
                return
            t.nw = max(lo, min(1.0, v)) - t.nx
            return
        prev, nxt = self.tiles[i - 1], self.tiles[i]
        right = nxt.nx + nxt.nw        # 读旧值(此时两个 Rect 均未修改)
        lo = max(MIN_TILE, prev.nx + MIN_TILE)   # prev.nx >= 0,恒等于 prev.nx + MIN_TILE
        hi = min(0.98, right - MIN_TILE)         # right <= 1,恒等于 right - MIN_TILE
        if hi < lo:
            return
        v = max(lo, min(hi, v))
        # 新值全部先算再写(不依赖"先保存旧右边界")
        prev.nw = v - prev.nx
        nxt.nx = v
        nxt.nw = right - v

    def copy(self) -> "GridLayout":
        """值拷贝(分割/段网格物化用;旧版 clone 的替代,值语义天然独立)。"""
        return GridLayout([Rect(t.nx, t.ny, t.nw, t.nh) for t in self.tiles])

    def check_invariants(self) -> None:
        """断言不变量:格子竖条、宽度 >= MIN_TILE、不相交、边缘线 [0,0.5]/[0.5,1]。"""
        n = len(self.tiles)
        assert n >= 2, f"格子过少: {n}"
        for t in self.tiles:
            assert t.nw >= MIN_TILE - 1e-9, f"格宽小于 {MIN_TILE}: {t}"
            assert abs(t.ny) < 1e-9 and abs(t.nh - 1) < 1e-9, f"非竖条: {t}"
        assert 0.0 <= self.crop_left <= 0.5, f"左边缘线越界: {self.crop_left}"
        assert 0.5 <= self.crop_right <= 1.0, f"右边缘线越界: {self.crop_right}"
        for a, b in zip(self.tiles, self.tiles[1:]):
            assert b.nx >= a.nx + a.nw - 1e-9, f"格子重叠: {a} vs {b}"

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {"tiles": [t.to_dict() for t in self.tiles]}

    @staticmethod
    def from_dict(d: dict) -> "GridLayout":
        return GridLayout([Rect.from_dict(t) for t in d.get("tiles", [])] if d else [])

    def __eq__(self, other) -> bool:
        if not isinstance(other, GridLayout):
            return NotImplemented
        return self.tiles == other.tiles

    def __repr__(self) -> str:
        return f"GridLayout({[round(t.nx, 3) for t in self.tiles]})"
