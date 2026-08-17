"""Scope:渲染/编辑的第一类作用域对象(隐式全段的显式化)。

旧版"无段 = 全部补丁/复制规则生效"的判断散落 8 处(effective_segments、
toggle 建显式段、绘制、命中、守卫、刷新……)。新版全部收敛为:
- 渲染只面对 planning.effective_scopes(project) 的连续覆盖序列
- UI/守卫/绘制/命中只问 AppState.current_scope()(单个调用点)
- 补丁创建时 anchor 赋值 = 当前 scope 的 grid_key(锚定实际解析的网格)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .grid import GridLayout
from .project import Project


@dataclass(frozen=True)
class Scope:
    """一个时间作用域:时间范围 + 该范围生效的补丁/复制规则 + 解析网格。"""

    key: str                            # "global" | "segment:<id>" | "gap:<start>-<end>"
    start: float
    end: float
    grid: GridLayout                    # 已解析的网格(值,段网格或全局网格)
    grid_key: str                       # "project" | "segment:<id>"(补丁/复制 anchor 派发)
    patch_ids: tuple = field(default_factory=tuple)
    copy_rule_ids: tuple = field(default_factory=tuple)
    start_frame: "int | None" = None    # 0 基帧号,None = 未知(VFR/旧工程)
    end_frame: "int | None" = None

    @property
    def frame_count(self) -> "int | None":
        if self.start_frame is not None and self.end_frame is not None:
            return max(0, self.end_frame - self.start_frame)
        return None

    @property
    def has_work(self) -> bool:
        """该 scope 是否启用了补丁或复制规则(边缘移动守卫的唯一数据源)。"""
        return bool(self.patch_ids or self.copy_rule_ids)

    @property
    def is_gap(self) -> bool:
        return self.key.startswith("gap:")

    def __repr__(self) -> str:
        return (f"Scope({self.key}, {self.start:.1f}-{self.end:.1f}, "
                f"{len(self.patch_ids)} 补丁, {len(self.copy_rule_ids)} 复制)")


def effective_scopes(project: Project, fps: "float | None" = None) -> list[Scope]:
    """返回覆盖 [process_range] 的连续 Scope 序列(用户段 + 自动补的空隙段)。

    无任何用户段 → 单个 global scope(全部补丁/复制规则,整个处理范围生效)。
    有段 → 段按 start 排序、裁剪到范围、重叠时后开始的段覆盖重叠区;
    首/尾/段间空隙自动补 gap scope(空规则,用全局网格)。

    fps 用于旧工程/无帧号字段时派生 0 基帧号;传媒体实际 fps。
    """
    lo, hi = project.process_range
    lo_frame, hi_frame = (project.process_range_frames
                          if project.process_range_frames is not None
                          else (_frame(lo, fps), _frame(hi, fps)))
    if not project.segments:
        return [Scope("global", lo, hi, project.grid, "project",
                      tuple(p.id for p in project.patches),
                      tuple(r.id for r in project.copy_rules),
                      lo_frame, hi_frame)]

    out: list[Scope] = []
    cur = lo
    cur_frame = lo_frame
    for seg in sorted(project.segments, key=lambda s: s.start):
        s, e = max(seg.start, lo), min(seg.end, hi)
        if e <= cur or e <= s:
            continue
        seg_start_frame = (seg.start_frame if seg.start_frame is not None
                           else _frame(seg.start, fps))
        seg_end_frame = (seg.end_frame if seg.end_frame is not None
                         else _frame(seg.end, fps))
        # 片段被处理范围裁剪:只保留范围内部分,帧边界同步裁剪
        if abs(s - seg.start) > 1e-9 and seg_start_frame is not None:
            seg_start_frame = max(seg_start_frame, lo_frame if lo_frame is not None else 0)
        if abs(e - seg.end) > 1e-9 and seg_end_frame is not None:
            seg_end_frame = min(seg_end_frame, hi_frame if hi_frame is not None else seg_end_frame)
        if s > cur + 1e-9:
            out.append(_gap_scope(cur, s, project, cur_frame, seg_start_frame))
        grid = seg.grid if seg.grid is not None else project.grid
        grid_key = f"segment:{seg.id}" if seg.grid is not None else "project"
        out.append(Scope(f"segment:{seg.id}", max(s, cur), e, grid, grid_key,
                         tuple(seg.patch_ids), tuple(seg.copy_rule_ids),
                         seg_start_frame, seg_end_frame))
        cur = max(cur, e)
        cur_frame = seg_end_frame
    if cur < hi:
        out.append(_gap_scope(cur, hi, project, cur_frame, hi_frame))
    return out


def _frame(t: float, fps: "float | None") -> "int | None":
    if fps and fps > 0:
        return int(round(t * fps))
    return None


def _gap_scope(start: float, end: float, project: Project,
               start_frame: "int | None" = None,
               end_frame: "int | None" = None) -> Scope:
    return Scope(f"gap:{start:.3f}-{end:.3f}", start, end,
                 project.grid, "project", (), (), start_frame, end_frame)
