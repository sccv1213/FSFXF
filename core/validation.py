"""校验:返回 issue 列表(空 = 可渲染)。

报错信息用"补丁N/复制规则N"序号命名,不用 uuid(用户明确要求)。
补丁网格一律经 Project.resolve_grid(p.anchor_grid) 解析(全项目唯一路径)。
"""
from __future__ import annotations

from .project import Project


def validate(project: Project) -> list[str]:
    """返回问题列表(空 = 可渲染)。"""
    issues: list[str] = []
    if not project.video_path or not project.duration:
        issues.append("尚未打开视频")
    lo, hi = project.process_range
    for s in project.segments:
        if s.end <= s.start:
            issues.append(f"片段 [{s.start:.1f}s, {s.end:.1f}s] 起止无效")
        if s.start < lo - 1e-6 or s.end > hi + 1e-6:
            issues.append(f"片段 [{s.start:.1f}s, {s.end:.1f}s] 超出处理范围")
    for i in range(len(project.segments)):
        for j in range(i + 1, len(project.segments)):
            a, b = project.segments[i], project.segments[j]
            if a.start < b.end and b.start < a.end:
                issues.append(f"片段 [{a.start:.1f}s, {a.end:.1f}s] 与 [{b.start:.1f}s, {b.end:.1f}s] 重叠")
    for i, p in enumerate(project.patches):
        name = f"补丁{i + 1}"   # 序号命名,方便用户定位
        for label, r in (("目标", p.dst), ("源", p.src)):
            if r.nw <= 0 or r.nh <= 0:
                issues.append(f"{name} 的{label}区域无效")
        # 网格验证用补丁归属的网格(resolve_grid 统一解析路径)
        g = project.resolve_grid(p.anchor_grid)
        if g.tiles:
            idx = g.tile_index_at(*p.dst.center())
            if idx is None:
                issues.append(f"{name} 的目标区域不在任何格子内")
            else:
                t = g.tiles[idx]
                eps = 1e-6
                if (p.dst.nx < t.nx - eps or p.dst.nx + p.dst.nw > t.nx + t.nw + eps
                        or p.dst.ny < t.ny - eps or p.dst.ny + p.dst.nh > t.ny + t.nh + eps):
                    issues.append(f"{name} 的目标区域跨出格子边界(请拆成两个补丁)")
            # 尺寸保持后 src 可能越出来源格(源格过窄)→ 提示用户,不静默兜底
            if p.lock_align and p.source_tile_idx >= 0:
                t_src = g.tile(p.source_tile_idx)
                if t_src is not None and not (
                        t_src.contains(p.src.nx, p.src.ny)
                        and t_src.contains(p.src.nx + p.src.nw, p.src.ny + p.src.nh)):
                    issues.append(f"{name} 的源区域跨出来源格边界(格子过窄,请移回分界线)")
            if p.dst_tile_idx >= 0:
                t = g.tile(p.dst_tile_idx)
                if t is None:
                    issues.append(f"{name} 的目标格索引无效")
                elif not (t.contains(p.dst.nx, p.dst.ny)
                          and t.contains(p.dst.nx + p.dst.nw, p.dst.ny + p.dst.nh)):
                    issues.append(f"{name} 的目标区域不在所选目标格内")
            for ti in p.extra_tile_indices:
                if ti < 0 or (g.tiles and ti >= len(g.tiles)):
                    issues.append(f"{name} 的额外目标格索引无效")
                elif ti == p.source_tile_idx:
                    issues.append(f"{name} 的额外目标格不能是来源格")
            if p.lock_align and p.source_tile_idx < 0:
                issues.append(f"{name} 未指定来源格子")
    # ---- 复制规则校验(按规则 anchor_grid 解析网格,与渲染/UI 同源) ----
    for i, r in enumerate(project.copy_rules):
        rname = f"复制规则{i + 1}"
        if not r.target_tile_indices:
            issues.append(f"{rname} 未选择目标格子")
        if r.source_tile_idx in r.target_tile_indices:
            issues.append(f"{rname} 的目标格包含来源格")
        g = project.resolve_grid(r.anchor_grid)
        if r.source_tile_idx >= len(g.tiles):
            issues.append(f"{rname} 的来源格超出锚定网格格数")
        for ti in r.target_tile_indices:
            if ti >= len(g.tiles):
                issues.append(f"{rname} 的目标格超出锚定网格格数")
    return issues
