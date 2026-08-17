"""归一化矩形 + 像素转换。

坐标一律归一化(0..1,相对视频帧),与显示缩放/DPI 无关;
转像素统一经 Rect.to_px() 取整并取偶(yuv420p 下 crop/overlay 奇数宽高会报错)。

等宽性(结构性不变式):x 与 w 各自 round 后取偶,宽 = round(nw*W) & ~1
只取决于 nw——归一化等宽的两个矩形(补丁 src/dst)在像素层必然等宽。
(曾试验"取偶联动"x2=round((nx+nw)*W),发现 w 随 nx 浮动破坏等宽性,
已放弃;渲染端 min(sw, dw) 保留为防御兜底,见 CLAUDE.md)
"""
from __future__ import annotations


class Rect:
    """归一化矩形(nx, ny, nw, nh ∈ [0,1])。"""

    __slots__ = ("nx", "ny", "nw", "nh")

    def __init__(self, nx: float, ny: float, nw: float, nh: float):
        self.nx = nx
        self.ny = ny
        self.nw = nw
        self.nh = nh

    # ---- 构造 ----
    @staticmethod
    def from_dict(d: dict) -> "Rect":
        return Rect(d["nx"], d["ny"], d["nw"], d["nh"])

    # ---- 转换 ----
    def to_px(self, frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
        """转像素坐标 (x, y, w, h):取整 + 取偶 + 裁剪到画面内。

        宽高 ≤0 返回 (0,0,0,0)(零尺寸 = 补丁停用的显式标记)。
        """
        x = int(round(self.nx * frame_w)) & ~1
        y = int(round(self.ny * frame_h)) & ~1
        w = int(round(self.nw * frame_w)) & ~1
        h = int(round(self.nh * frame_h)) & ~1
        if w <= 0 or h <= 0:
            return (0, 0, 0, 0)
        x = max(0, min(x, frame_w - w))
        y = max(0, min(y, frame_h - h))
        return (x, y, w, h)

    def center(self) -> tuple[float, float]:
        return (self.nx + self.nw / 2, self.ny + self.nh / 2)

    def contains(self, nx: float, ny: float) -> bool:
        return self.nx <= nx <= self.nx + self.nw and self.ny <= ny <= self.ny + self.nh

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {"nx": self.nx, "ny": self.ny, "nw": self.nw, "nh": self.nh}

    def __eq__(self, other) -> bool:
        if not isinstance(other, Rect):
            return NotImplemented
        return (abs(self.nx - other.nx) < 1e-9 and abs(self.ny - other.ny) < 1e-9
                and abs(self.nw - other.nw) < 1e-9 and abs(self.nh - other.nh) < 1e-9)

    def __repr__(self) -> str:
        return f"Rect({self.nx:.3f}, {self.ny:.3f}, {self.nw:.3f}, {self.nh:.3f})"
