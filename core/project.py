"""数据模型:补丁/片段/工程 + JSON 持久化(version=3,兼容 v2 迁移,旧 v1 拒绝)。

坐标一律归一化(0..1),转像素统一经 Rect.to_px。

归属模型(替代旧版"对象引用即所有权"):可序列化、无引用计数
- Patch.anchor_grid: "project" | "segment:<seg_id>",补丁锚定创建时实际解析的网格
- Segment.grid: None = 显式继承 Project.grid(值语义,非共享引用)
- 解析唯一路径:Project.resolve_grid(anchor_grid);悬空 anchor(段已删除)回退
  全局网格,补丁标 stale(设计决定:与旧"冻结快照"不同,见 CLAUDE.md)
- realign_patches(grid_key):字符串比较派发,替代旧 `p.grid is grid` 身份过滤

网格表示:GridLayout.edges(分界线列表),crop_left/right 派生自首/尾边,
set_crop 是裁剪唯一写入口(全网格同步,替代 apply_outer_edges 三处重复)。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from .grid import GridLayout
from .rect import Rect

FORMAT = "fenshenfu"
VERSION = 3

# 工程 JSON 迁移注册表:{(from_version, to_version): migrate(dict)->dict}
# v2 → v3:新增帧号/翻转/锚定等字段,读取端已有缺省兼容,迁移只需标版本。
def _migrate_project_v2_to_v3(data: dict) -> dict:
    out = dict(data)
    out["version"] = 3
    out.setdefault("process_range_frames", None)
    out.setdefault("process_range_pts_ticks", None)
    out.setdefault("video_time_base", None)
    return out


_MIGRATION_STEPS: dict[tuple[int, int], object] = {
    (2, 3): _migrate_project_v2_to_v3,
}


def migrate_project_dict(data: dict) -> dict:
    """按注册表把工程 dict 迁移到当前 VERSION。

    旧 version=1 没有迁移步骤 → 明确拒绝(用户确认:旧档请重建)。
    """
    fmt = data.get("format")
    version = data.get("version")
    if fmt != FORMAT:
        raise ValueError(f"不兼容的工程文件(format={fmt!r}, version={version!r}, "
                         f"期望 {FORMAT}/{VERSION});旧版工程请在新版中重建")
    if not isinstance(version, int):
        raise ValueError(f"工程文件缺少有效 version(={version!r});期望 {FORMAT}/{VERSION}")
    while version < VERSION:
        step = _MIGRATION_STEPS.get((version, version + 1))
        if step is None:
            raise ValueError(f"不兼容的工程文件(format={fmt!r}, version={version!r}, "
                             f"期望 {FORMAT}/{VERSION});旧版工程请在新版中重建")
        data = step(data)
        version += 1
    if version != VERSION:
        raise ValueError(f"不兼容的工程文件(format={fmt!r}, version={version!r}, "
                         f"期望 {FORMAT}/{VERSION})")
    return data


def new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Patch:
    """一个修复补丁:从 src(干净区域)复制像素盖到 dst(脏区域)。"""

    id: str = field(default_factory=new_id)
    anchor_grid: str = "project"   # 归属网格键:"project" | "segment:<seg_id>"
    dst: Rect = field(default_factory=lambda: Rect(0, 0, 0, 0))
    src: Rect = field(default_factory=lambda: Rect(0, 0, 0, 0))
    source_tile_idx: int = -1      # 来源格索引;-1 = 手动设置(未用网格自动对齐)
    dst_tile_idx: int = -1         # 主目标格索引;-1 = 由几何推断
    extra_tile_indices: list = field(default_factory=list)   # 额外目标格(多目标补丁)
    lock_align: bool = True        # True 时 src 由网格相对映射自动派生,改动 dst 会重算 src

    def target_tile_indices(self) -> list:
        """全部目标格(主 + 额外)。"""
        out = []
        if self.dst_tile_idx >= 0:
            out.append(self.dst_tile_idx)
        for i in self.extra_tile_indices:
            if i not in out:
                out.append(i)
        return out

    def to_dict(self) -> dict:
        return {"id": self.id, "anchor_grid": self.anchor_grid,
                "dst": self.dst.to_dict(), "src": self.src.to_dict(),
                "source_tile_idx": self.source_tile_idx,
                "dst_tile_idx": self.dst_tile_idx,
                "extra_tile_indices": list(self.extra_tile_indices),
                "lock_align": self.lock_align}

    @staticmethod
    def from_dict(d: dict) -> "Patch":
        return Patch(id=d.get("id", new_id()),
                     anchor_grid=d.get("anchor_grid", "project"),
                     dst=Rect.from_dict(d["dst"]), src=Rect.from_dict(d["src"]),
                     source_tile_idx=d.get("source_tile_idx", -1),
                     dst_tile_idx=d.get("dst_tile_idx", -1),
                     extra_tile_indices=list(d.get("extra_tile_indices", [])),
                     lock_align=d.get("lock_align", True))

    def __repr__(self) -> str:
        return f"Patch({self.id}, dst={self.dst}, src={self.src}, tile={self.source_tile_idx})"


@dataclass
class Segment:
    """时间段 [start, end](秒),启用全局补丁/复制规则列表中的部分项。

    id 是 anchor 引用的基础(旧版无 id,重写新增;分割出的新段必是新 id)。
    start_frame/end_frame:0 基帧号(CFR 下为帧精确边界;旧工程缺省 None,
    由 start/end × fps 派生)。
    """

    start: float
    end: float
    id: str = field(default_factory=new_id)
    patch_ids: list[str] = field(default_factory=list)
    copy_rule_ids: list[str] = field(default_factory=list)   # 该段启用的复制规则
    grid: "GridLayout | None" = None   # 该段自己的布局;None = 继承 Project.grid
    start_frame: "int | None" = None
    end_frame: "int | None" = None

    def to_dict(self) -> dict:
        return {"id": self.id, "start": self.start, "end": self.end,
                "patch_ids": list(self.patch_ids),
                "copy_rule_ids": list(self.copy_rule_ids),
                "grid": self.grid.to_dict() if self.grid else None,
                "start_frame": self.start_frame, "end_frame": self.end_frame}

    @staticmethod
    def from_dict(d: dict) -> "Segment":
        gd = d.get("grid")
        return Segment(id=d.get("id", new_id()),
                       start=d["start"], end=d["end"],
                       patch_ids=list(d.get("patch_ids", [])),
                       copy_rule_ids=list(d.get("copy_rule_ids", [])),
                       grid=GridLayout.from_dict(gd) if gd else None,
                       start_frame=d.get("start_frame"),
                       end_frame=d.get("end_frame"))

    def __repr__(self) -> str:
        return (f"Segment({self.start:.1f}-{self.end:.1f}, "
                f"{len(self.patch_ids)} 补丁, {len(self.copy_rule_ids)} 复制)")


@dataclass
class CopyRule:
    """复制格子规则:把来源格整格内容复制到多个目标格(不能选来源格自身)。

    anchor_grid:规则锚定的网格键("project" | "segment:<seg_id>")。
    与补丁一致:规则创建后始终用该网格解析格索引,不随启用片段变化。
    flip_horizontal:勾选后所有目标格接收水平镜像的来源格画面。
    """

    id: str = field(default_factory=new_id)
    source_tile_idx: int = 0
    target_tile_indices: list = field(default_factory=list)
    anchor_grid: str = "project"
    flip_horizontal: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "source_tile_idx": self.source_tile_idx,
                "target_tile_indices": list(self.target_tile_indices),
                "anchor_grid": self.anchor_grid,
                "flip_horizontal": self.flip_horizontal}

    @staticmethod
    def from_dict(d: dict) -> "CopyRule":
        return CopyRule(id=d.get("id", new_id()),
                        source_tile_idx=d.get("source_tile_idx", 0),
                        target_tile_indices=list(d.get("target_tile_indices", [])),
                        anchor_grid=d.get("anchor_grid", "project"),
                        flip_horizontal=d.get("flip_horizontal", False))

    def __repr__(self) -> str:
        return (f"CopyRule({self.id}, 格{self.source_tile_idx + 1} → "
                f"{[t + 1 for t in self.target_tile_indices]})")


@dataclass
class EncoderSettings:
    """编码设置。"""

    encoder_mode: str = "hw"          # "hw" 硬件优先(默认, NVENC) | "sw" 纯软件
    quality_mode: str = "match"       # "match" 匹配原码率(默认) | "crf" | "custom"
    crf: int = 18
    custom_kbps: int = 6000
    factor: float = 1.0               # 码率系数(match 模式微调)
    preset: str = "medium"            # 软件编码 preset;NVENC 固定 p6
    audio_reencode: bool = False      # 默认音频流复制(零损失)
    audio_kbps: int = 0               # 0 = 匹配源码率
    output_dir: str = ""              # 空 = 源视频所在目录

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict) -> "EncoderSettings":
        s = EncoderSettings()
        for k in s.__dict__:
            if k in d:
                setattr(s, k, d[k])
        return s


class Project:
    """一个视频的完整工程配置。"""

    def __init__(self, video_path: str = "", duration: float = 0.0):
        self.video_path = video_path
        self.duration = duration
        self.process_range: list[float] = [0.0, duration]   # [in, out](秒)
        self.process_range_frames: "list[int] | None" = None   # 0 基帧号 [start,end)
        self.process_range_pts_ticks: "list[int] | None" = None   # VFR 精确边界(流 ticks)
        self.video_time_base: "tuple[int, int] | None" = None   # 视频流 time_base
        self.frame_index = None   # 运行期 FrameIndex,不序列化
        self.revision = 0        # 结构版本号(scope 缓存失效依据)
        self.grid: GridLayout = GridLayout()   # 唯一全局网格;edges[0]/[-1] 是唯一 crop 真源
        self.segments: list[Segment] = []
        self.patches: list[Patch] = []
        self.copy_rules: list[CopyRule] = []
        self.settings = EncoderSettings()

    # ---- 变更版本 ----
    def bump(self) -> None:
        """任何影响 effective_scopes 的结构变化后 +1。"""
        self.revision += 1

    # ---- 查询 ----
    def patch(self, pid: str) -> Patch | None:
        for p in self.patches:
            if p.id == pid:
                return p
        return None

    def copy_rule(self, rid: str) -> CopyRule | None:
        for r in self.copy_rules:
            if r.id == rid:
                return r
        return None

    def segment_at(self, t: float) -> Segment | None:
        for s in self.segments:
            if s.start <= t < s.end:
                return s
        return None

    def segment_grid(self, seg: "Segment | None") -> GridLayout:
        """片段使用的网格:段自己的网格,无则用全局网格。"""
        if seg is not None and seg.grid is not None:
            return seg.grid
        return self.grid

    def resolve_grid(self, anchor_grid: str) -> GridLayout:
        """解析补丁归属网格(全项目唯一路径,任何 UI/渲染/校验都不得自行拼解析)。

        "project" → 全局网格;"segment:<id>" → 段有网格用段网格,无网格继承全局;
        段不存在(anchor 悬空)→ 回退全局网格(补丁由 stale_anchors() 标记)。
        """
        if anchor_grid == "project":
            return self.grid
        if anchor_grid.startswith("segment:"):
            sid = anchor_grid[len("segment:"):]
            for s in self.segments:
                if s.id == sid:
                    return s.grid if s.grid is not None else self.grid
        return self.grid

    def stale_anchors(self) -> set[str]:
        """anchor 悬空(归属段已删除)的补丁/复制规则 anchor_grid 集合。"""
        ids = {s.id for s in self.segments}
        out = {p.anchor_grid for p in self.patches
               if p.anchor_grid.startswith("segment:")
               and p.anchor_grid[len("segment:"):] not in ids}
        out.update(r.anchor_grid for r in self.copy_rules
                   if r.anchor_grid.startswith("segment:")
                   and r.anchor_grid[len("segment:"):] not in ids)
        return out

    # ---- 对齐 ----
    def realign_patches(self, grid_key: str = "project") -> None:
        """网格几何变化后重算对齐。只重算 anchor_grid == grid_key 的 lock 补丁
        (字符串比较,替代旧版 `p.grid is grid` 对象身份过滤——切布局后
        段网格换新,该段补丁跟随新网格重算;差异记录见 CLAUDE.md)。"""
        for p in self.patches:
            if not p.lock_align or p.anchor_grid != grid_key:
                continue
            g = self.resolve_grid(p.anchor_grid)
            if not g.tiles:
                continue
            pair = g.align_rect_pair(p.dst, p.source_tile_idx)
            if pair is not None:
                p.src, p.dst = pair   # (src, dst) 同尺寸对(含格内收窄)

    # ---- 网格编辑(唯一写入口) ----
    def set_crop(self, lo: float, hi: float) -> None:
        """裁剪边界唯一写入口:改全局网格首/尾边 + 所有段网格(边缘线全程统一)。

        clamp 全部收敛在 GridLayout.move_edge 一处。
        """
        self.bump()
        self.grid.move_edge(0, lo)
        self.grid.move_edge(len(self.grid.tiles), hi)
        for s in self.segments:
            if s.grid is not None:
                s.grid.move_edge(0, lo)
                s.grid.move_edge(len(s.grid.tiles), hi)

    def apply_frame_index(self, index) -> None:
        """把真实帧索引写入工程边界(打开视频/索引建立后调用)。

        - CFR:index 也可纠正 timebase 不是 1/fps 的累计漂移
        - VFR:帧号与时间边界都改为真实 pts,不再使用平均 fps 换算
        """
        if index is None or index.frame_count <= 0:
            return
        self.bump()
        self.video_time_base = (index.time_base.numerator,
                                index.time_base.denominator)
        self.frame_index = index
        lo_frame = index.frame_at_or_after(self.process_range[0])
        hi_frame = index.frame_at_or_after(self.process_range[1])
        if hi_frame <= lo_frame:
            hi_frame = min(index.frame_count, lo_frame + 1)
        self.process_range_frames = [lo_frame, hi_frame]
        self.process_range_pts_ticks = [index.pts_ticks(lo_frame),
                                        index.end_ticks(hi_frame)]
        self.process_range = [index.seconds(lo_frame),
                              float(index.end_ticks(hi_frame) * index.time_base)]
        for seg in self.segments:
            sf = index.frame_at_or_after(seg.start)
            ef = index.frame_at_or_after(seg.end)
            if ef <= sf:
                ef = min(index.frame_count, sf + 1)
            seg.start_frame = sf
            seg.end_frame = ef
            seg.start = index.seconds(sf)
            seg.end = float(index.end_ticks(ef) * index.time_base)

    def materialize_segment_grid(self, seg: Segment) -> bool:
        """段内边界编辑前调用:段网格为 None(继承全局)→ 克隆一份给当前段。

        返回 True 表示发生了物化(调用方需刷新上下文)。值语义,无引用计数。
        """
        if seg.grid is None:
            self.bump()
            seg.grid = self.grid.copy()
            return True
        return False

    # ---- 片段操作 ----
    def _frame_at(self, t: float, fps: float) -> "int | None":
        if fps and fps > 0:
            return int(round(t * fps))
        return None

    def split_at(self, t: float, fps: float) -> None:
        """在 t 处分割。

        t 使用调用方传入的实际 pts(播放头所在帧),不再用 round(t*fps)/fps
        覆盖它——timebase 不是 1/fps 时,后者会偏离真实帧边界(丢/重帧)。
        同时保存 0 基帧号 start_frame/end_frame,渲染端据此强制 -frames:v。

        永久分段条 = 处理范围边界 [lo, hi],是第一个分段的开头与最后一个
        分段的结尾:
        - 分割点必须在 (lo, hi) 内,范围外无法添加分段
        - seg 不在任何段内(无段或段外空隙)→ 在空隙 [a,b] 内插入两段
          [a,t][t,b],边界 a/b 默认取 lo/hi(空隙跨处理范围边界时从永久条
          开始);各含全部补丁/复制规则 id 且 list() 独立拷贝,不覆盖现有分段
        - 有段命中 → 两段各自克隆网格,后续分界线独立编辑互不影响
        分割出的新段必是新 id(anchor 引用安全)。
        """
        self.bump()
        t = float(t)
        t_frame = self._frame_at(t, fps)
        lo, hi = self.process_range
        if t <= lo or t >= hi:
            return   # 分割点必须在永久分段条(处理范围)之间
        seg = self.segment_at(t)
        if seg is None:
            # 找 t 所在的空隙 [a, b](a = 前一段尾或 lo,b = 下一段头或 hi)
            a, b = lo, hi
            a_frame, b_frame = None, None
            for s in sorted(self.segments, key=lambda s: s.start):
                if s.end <= t:
                    a = max(a, s.end)
                    a_frame = (s.end_frame if s.end_frame is not None
                               else self._frame_at(s.end, fps))
                elif s.start >= t:
                    b = min(b, s.start)
                    b_frame = (s.start_frame if s.start_frame is not None
                               else self._frame_at(s.start, fps))
                    break
            if b - a <= 0:
                return
            if a_frame is None:
                if self.process_range_frames is not None and abs(a - lo) < 1e-6:
                    a_frame = self.process_range_frames[0]
                else:
                    a_frame = self._frame_at(a, fps)
            if b_frame is None:
                if self.process_range_frames is not None and abs(b - hi) < 1e-6:
                    b_frame = self.process_range_frames[1]
                else:
                    b_frame = self._frame_at(b, fps)
            all_p = [p.id for p in self.patches]
            all_r = [r.id for r in self.copy_rules]
            self.segments += [Segment(id=new_id(), start=a, end=t,
                                      patch_ids=list(all_p), copy_rule_ids=list(all_r),
                                      grid=None, start_frame=a_frame, end_frame=t_frame),
                              Segment(id=new_id(), start=t, end=b,
                                      patch_ids=list(all_p), copy_rule_ids=list(all_r),
                                      grid=None, start_frame=t_frame, end_frame=b_frame)]
            self.segments.sort(key=lambda s: s.start)   # 保持有序(segment_at 语义)
            return
        if abs(seg.start - t) < 1e-6 or abs(seg.end - t) < 1e-6:
            return
        seg_start_frame = (seg.start_frame if seg.start_frame is not None
                           else self._frame_at(seg.start, fps))
        seg_end_frame = (seg.end_frame if seg.end_frame is not None
                         else self._frame_at(seg.end, fps))
        if (t_frame is not None and seg_start_frame is not None
                and not (seg_start_frame < t_frame < seg_end_frame)):
            return
        idx = self.segments.index(seg)
        g0 = seg.grid.copy() if seg.grid is not None else None
        g1 = seg.grid.copy() if seg.grid is not None else None
        self.segments[idx:idx + 1] = [
            Segment(id=new_id(), start=seg.start, end=t,
                    patch_ids=list(seg.patch_ids), copy_rule_ids=list(seg.copy_rule_ids),
                    grid=g0, start_frame=seg_start_frame, end_frame=t_frame),
            Segment(id=new_id(), start=t, end=seg.end,
                    patch_ids=list(seg.patch_ids), copy_rule_ids=list(seg.copy_rule_ids),
                    grid=g1, start_frame=t_frame, end_frame=seg_end_frame)]

    def remove_segment_merge(self, index: int) -> None:
        """删除分段线:删线右侧段,并与其左侧段合并为新段(两侧两段合一)。

        语义 = UI"删除分段线"(分段线 = 下一段起点,segment_at 半开区间
        返回右侧段):合并范围 = X + 左邻(含中间空隙),右邻保持独立;新段
        patch_ids/copy_rule_ids 为空、grid=None(继承全局)。曾无条件吸收
        左右两邻(三连合并)——删线场景中被删段左右都有邻居时会多吞一段,
        跨度恰为整个处理范围时整段删光(用户报告:删线3 后删线1 把剩余段
        全合并成全长)。若合并结果覆盖整个 process_range → 直接删除(回到
        无段全局模式)——两段删一段的典型场景。补丁/复制全局列表不受影响。
        """
        self.bump()
        if not (0 <= index < len(self.segments)):
            return
        x = self.segments[index]
        lo, hi = self.process_range
        # 左邻:end <= x.start 中 start 最大(线左侧的段)
        left = None
        for s in self.segments:
            if s is x:
                continue
            if s.end <= x.start + 1e-9 and (left is None or s.start > left.start):
                left = s
        if left is None:
            # 无左邻(首段,UI 删除分段线不可达此路径):向后合并右邻保持
            # 对称;唯一段直接删除(回到无段全局模式)
            right = None
            for s in self.segments:
                if s is x:
                    continue
                if s.start >= x.end - 1e-9 and (right is None or s.start < right.start):
                    right = s
            if right is None:
                self.segments = []
                return
            new_start, new_end = x.start, right.end
            new_start_frame = x.start_frame
            new_end_frame = right.end_frame
            self.segments = [s for s in self.segments if s is not x and s is not right]
        else:
            new_start, new_end = left.start, x.end
            new_start_frame = left.start_frame
            new_end_frame = x.end_frame
            self.segments = [s for s in self.segments if s is not x and s is not left]
        if abs(new_start - lo) < 1e-6 and abs(new_end - hi) < 1e-6:
            return   # 合并覆盖整个处理范围 → 无段(隐式全段)
        self.segments.append(Segment(id=new_id(), start=new_start, end=new_end, grid=None,
                                     start_frame=new_start_frame, end_frame=new_end_frame))
        self.segments.sort(key=lambda s: s.start)

    def materialize_global_segment(self, exclude_patch: str | None = None,
                                   exclude_copy: str | None = None) -> None:
        """全局模式(无段)取消某项 → 建覆盖 process_range 的显式段(含其余全部 id)。

        隐式全段 → 显式段的唯一转换入口(替代旧 _on_patch_toggled 散落逻辑)。
        """
        self.bump()
        lo, hi = self.process_range
        lo_frame, hi_frame = (self.process_range_frames
                              if self.process_range_frames is not None
                              else (None, None))
        self.segments.append(Segment(
            new_id(), lo, hi,
            [p.id for p in self.patches if p.id != exclude_patch],
            [r.id for r in self.copy_rules if r.id != exclude_copy], None,
            lo_frame, hi_frame))

    # ---- 补丁/复制规则操作(seg None = 全局模式) ----
    def toggle_patch_in_segment(self, pid: str, on: bool, seg: Segment | None) -> None:
        self.bump()
        if seg is None:
            if not on:
                self.materialize_global_segment(exclude_patch=pid)
            return
        if on:
            if pid not in seg.patch_ids:
                seg.patch_ids.append(pid)
        elif pid in seg.patch_ids:
            seg.patch_ids.remove(pid)

    def toggle_copy_in_segment(self, rid: str, on: bool, seg: Segment | None) -> None:
        self.bump()
        if seg is None:
            if not on:
                self.materialize_global_segment(exclude_copy=rid)
            return
        if on:
            if rid not in seg.copy_rule_ids:
                seg.copy_rule_ids.append(rid)
        elif rid in seg.copy_rule_ids:
            seg.copy_rule_ids.remove(rid)

    def add_patch_to_context(self, patch: Patch, seg: Segment | None) -> None:
        """补丁创建:加入全局列表 + 有段则加入播放头所在段的启用列表(无段时隐式全段自动包含)。"""
        self.bump()
        self.patches.append(patch)
        if seg is not None and patch.id not in seg.patch_ids:
            seg.patch_ids.append(patch.id)

    def add_copy_to_context(self, rule: CopyRule, seg: Segment | None) -> None:
        """复制规则创建:加入全局列表 + 有段则加入当前段(无段时隐式全段自动包含)。

        anchor_grid 由调用方(AppState)在创建时按当前 scope 赋值。
        """
        self.bump()
        self.copy_rules.append(rule)
        if seg is not None and rule.id not in seg.copy_rule_ids:
            seg.copy_rule_ids.append(rule.id)

    def remove_patch(self, pid: str) -> None:
        self.bump()
        self.patches = [p for p in self.patches if p.id != pid]
        for s in self.segments:
            if pid in s.patch_ids:
                s.patch_ids.remove(pid)

    def remove_copy_rule(self, rid: str) -> None:
        self.bump()
        self.copy_rules = [r for r in self.copy_rules if r.id != rid]
        for s in self.segments:
            if rid in s.copy_rule_ids:
                s.copy_rule_ids.remove(rid)

    # ---- 模板 ----
    def apply_template(self, template: dict, seg: "Segment | None" = None) -> None:
        """应用模板(替换目标现有补丁/复制/网格)。

        seg = None → 替换全局(project.grid + 全部补丁/复制);
        seg = 段 → 替换该段(段网格物化为模板网格 + 该段启用的补丁/复制)。
        模板补丁/复制克隆新 id 且补丁 anchor = 目标 key——多段应用同一
        模板几何独立、删除单段互不影响。完成后 lock 补丁按目标网格重算。
        """
        self.bump()
        target_key = f"segment:{seg.id}" if seg is not None else "project"
        if seg is not None:
            seg.grid = template["grid"].copy()          # 段物化模板网格
            seg.patch_ids = []
            seg.copy_rule_ids = []
        else:
            self.grid = template["grid"].copy()
            self.patches = []
            self.copy_rules = []
        if seg is not None:
            # 段锚定补丁/复制清空(全局列表中的孤儿项无害,渲染按段启用列表走)
            self.patches = [p for p in self.patches if p.anchor_grid != target_key]
            self.copy_rules = [r for r in self.copy_rules if r.anchor_grid != target_key]
        for t in template["patches"]:
            p = Patch.from_dict({**t.to_dict(), "id": new_id(),
                                 "anchor_grid": target_key})
            self.patches.append(p)
            if seg is not None:
                seg.patch_ids.append(p.id)
        for t in template["copy_rules"]:
            r = CopyRule.from_dict({**t.to_dict(), "id": new_id(),
                                    "anchor_grid": target_key})
            self.copy_rules.append(r)
            if seg is not None:
                seg.copy_rule_ids.append(r.id)
        self.realign_patches(target_key)

    def clone_for_video(self, video_path: str, duration: float) -> "Project":
        """批量处理:按当前工程设置构造新视频工程(无段、范围全长、补丁/复制锚定全局)。"""
        pr = Project.from_dict(self.to_dict())
        pr.video_path = video_path
        pr.duration = duration
        pr.process_range = [0.0, duration]
        pr.process_range_frames = None   # 新视频帧数由批量导入方探测后设置
        pr.segments = []
        for p in pr.patches:
            p.anchor_grid = "project"
        for r in pr.copy_rules:
            r.anchor_grid = "project"
        return pr

    # ---- 校验(委托 validation 模块,保持 pr.validate() 调用方式) ----
    def validate(self) -> list[str]:
        from .validation import validate
        return validate(self)

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {
            "format": FORMAT,
            "version": VERSION,
            "video_path": self.video_path,
            "duration": self.duration,
            "process_range": list(self.process_range),
            "process_range_frames": (list(self.process_range_frames)
                                     if self.process_range_frames is not None
                                     else None),
            "process_range_pts_ticks": (list(self.process_range_pts_ticks)
                                        if self.process_range_pts_ticks is not None
                                        else None),
            "video_time_base": (list(self.video_time_base)
                                if self.video_time_base is not None else None),
            "grid": self.grid.to_dict(),
            "segments": [s.to_dict() for s in self.segments],
            "patches": [p.to_dict() for p in self.patches],
            "copy_rules": [r.to_dict() for r in self.copy_rules],
            "settings": self.settings.to_dict(),
        }

    @staticmethod
    def from_dict(d: dict) -> "Project":
        d = migrate_project_dict(d)
        pr = Project(d.get("video_path", ""), d.get("duration", 0.0))
        pr.process_range = list(d.get("process_range", [0.0, pr.duration]))
        prf = d.get("process_range_frames")
        pr.process_range_frames = [int(v) for v in prf] if isinstance(prf, (list, tuple)) and len(prf) == 2 else None
        prt = d.get("process_range_pts_ticks")
        pr.process_range_pts_ticks = [int(v) for v in prt] if isinstance(prt, (list, tuple)) and len(prt) == 2 else None
        vtb = d.get("video_time_base")
        pr.video_time_base = (int(vtb[0]), int(vtb[1])) if isinstance(vtb, (list, tuple)) and len(vtb) == 2 else None
        pr.grid = GridLayout.from_dict(d.get("grid"))
        pr.segments = [Segment.from_dict(s) for s in d.get("segments", [])]
        pr.patches = [Patch.from_dict(p) for p in d.get("patches", [])]
        pr.copy_rules = [CopyRule.from_dict(r) for r in d.get("copy_rules", [])]
        pr.settings = EncoderSettings.from_dict(d.get("settings", {}))
        return pr

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @staticmethod
    def load(path: str) -> "Project":
        with open(path, "r", encoding="utf-8") as f:
            return Project.from_dict(json.load(f))
