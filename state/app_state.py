"""工作流状态:Stage/EditMode/播放头/选择/悬停 + QSettings 持久化。

旧版这些散落在 main_window(约 75 个方法);新版 UI 只问 AppState,
MainWindow 只做 UI 组装与信号接线。

核心查询 current_scope():播放头时刻的作用域(全局模式 = global scope;
段内 = 段 scope;间隙/范围外 = gap scope)——旧版散落 8 处的
"无段=全部生效/当前段生效"判断全部收敛为这一个调用点。

语义差异(记录于 CLAUDE.md):
- 间隙内显示/守卫按 gap scope(无补丁,允许移边界)——旧版 gap 按隐式全段
  显示全部补丁且禁止移边界
- 勾选/取消补丁仍按"播放头所在用户段"(与旧版一致,user_segment_at)
"""
from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QObject, QSettings, Signal

from core.frame_index import FrameIndex
from core.planning import Scope, effective_scopes
from core.project import Project
from services.batch_queue import Job


class Stage(Enum):
    NO_VIDEO = 0
    READY = 1


class EditMode(Enum):
    SELECT = "select"     # 选择(补丁编辑 + 网格线拖动整合)
    DST = "target"        # 目标(画脏区域)
    SRC = "source"        # 源(画干净区域)


class AppState(QObject):
    """应用工作流状态:UI 与服务的单一真源。"""

    project_changed = Signal()
    scope_changed = Signal()           # 播放头跨段/网格变化(面板/画面刷新)
    stage_changed = Signal()
    queue_changed = Signal()           # 队列增删(UI 刷新)

    def __init__(self, settings_org: str = "FenshenFu",
                 settings_app: str = "fenshenfu", parent=None):
        super().__init__(parent)
        self._settings = QSettings(settings_org, settings_app)
        self._project: Project | None = None
        self._video_meta = None
        self._stage = Stage.NO_VIDEO
        self._mode = EditMode.SELECT
        self._current_time = 0.0
        self._hover_target = "window"   # volume|frame|input|window
        self._scope_cache: Scope | None = None
        self._scopes_cache: list[Scope] | None = None
        self._scopes_cache_rev = -1
        self._scopes_cache_fps: float | None = None
        self._frame_index: FrameIndex | None = None
        self._queue_jobs: list[Job] = []

    # ---------- 派生查询(UI 只问这里) ----------
    @property
    def project(self) -> Project | None:
        return self._project

    @property
    def video_meta(self):
        return self._video_meta

    @property
    def frame_index(self) -> FrameIndex | None:
        return self._frame_index

    @property
    def stage(self) -> Stage:
        return self._stage

    @property
    def mode(self) -> EditMode:
        return self._mode

    @property
    def current_time(self) -> float:
        return self._current_time

    @property
    def hover_target(self) -> str:
        return self._hover_target

    @property
    def queue_jobs(self) -> list[Job]:
        return list(self._queue_jobs)

    def effective_scopes(self) -> list[Scope]:
        """当前工程的 effective_scopes 缓存(结构 revision 或 fps 变化才重建)。"""
        if self._project is None:
            return []
        fps = getattr(self._video_meta, "fps", None)
        if (self._scopes_cache is None or self._scopes_cache_rev != self._project.revision
                or self._scopes_cache_fps != fps):
            self._scopes_cache = effective_scopes(self._project, fps)
            self._scopes_cache_rev = self._project.revision
            self._scopes_cache_fps = fps
        return self._scopes_cache

    def current_scope(self) -> Scope | None:
        """播放头时刻的作用域(全局/段/间隙),UI 与渲染的唯一上下文查询。"""
        scopes = self.effective_scopes()
        if not scopes:
            return None
        t = self._current_time
        for s in scopes:
            if s.start <= t < s.end:
                return s
        # 播放头在 process_range 外 → 最近端作用域(内容一致,仅时间标签不同)
        return scopes[0] if t < scopes[0].start else scopes[-1]

    def _invalidate_scope_cache(self) -> None:
        self._scopes_cache = None
        self._scopes_cache_rev = -1

    def current_grid(self):
        """当前编辑上下文网格。"""
        scope = self.current_scope()
        return scope.grid if scope is not None else None

    def is_global_mode(self) -> bool:
        """隐式全段(无任何用户段)。"""
        return bool(self._project) and not self._project.segments

    def user_segment_at(self, t: float):
        """播放头所在用户段(勾选/取消用,与旧版一致);None = 全局或间隙。"""
        return self._project.segment_at(t) if self._project else None

    def current_user_segment(self):
        return self.user_segment_at(self._current_time)

    def can_render(self) -> tuple[bool, list[str]]:
        """校验门:未开视频/有 issue 不可渲染。"""
        if self._project is None:
            return False, ["尚未打开视频"]
        issues = self._project.validate()
        return (not issues, issues)

    def enabled_patch_ids(self) -> set[str]:
        """当前 scope 启用的补丁 id(面板勾选状态)。"""
        scope = self.current_scope()
        if scope is None:
            return set()
        return set(scope.patch_ids)

    def enabled_copy_ids(self) -> set[str]:
        scope = self.current_scope()
        if scope is None:
            return set()
        return set(scope.copy_rule_ids)

    # ---------- 持久化(QSettings 仅 last_dir/volume,测试注入 org/app) ----------
    def last_dir(self) -> str:
        return self._settings.value("last_dir", "")

    def set_last_dir(self, path: str) -> None:
        self._settings.setValue("last_dir", path)

    def volume(self) -> int:
        v = int(self._settings.value("volume", 70))
        return max(0, min(100, v))

    def set_volume(self, v: int) -> None:
        self._settings.setValue("volume", max(0, min(100, int(v))))

    def get_setting(self, key: str, default=""):
        """通用 QSettings 读取(last_dir/volume 之外的新键,如 splitter_sizes)。"""
        return self._settings.value(key, default)

    def set_setting(self, key: str, value) -> None:
        self._settings.setValue(key, value)

    # ---------- 动作(委托 Project 后发 change) ----------
    def set_video(self, project: Project, meta) -> None:
        self._project = project
        self._video_meta = meta
        self._frame_index = None
        if project.process_range_frames is None:
            fps = getattr(meta, "fps", 0.0) or 30.0
            if getattr(meta, "frame_count", 0) and abs(project.process_range[0]) < 1e-6 \
                    and abs(project.process_range[1] - project.duration) < 1e-3:
                project.process_range_frames = [0, int(meta.frame_count)]
            else:
                project.process_range_frames = [
                    int(round(project.process_range[0] * fps)),
                    int(round(project.process_range[1] * fps))]
        self._scope_cache = None
        self._invalidate_scope_cache()
        self._stage = Stage.READY
        self._current_time = 0.0
        self.project_changed.emit()
        self.scope_changed.emit()
        self.stage_changed.emit()

    def set_frame_index(self, index: FrameIndex) -> None:
        """后台帧索引就绪:工程边界改写为真实 PTS,并刷新 UI。"""
        if self._project is None:
            return
        self._frame_index = index
        self._project.apply_frame_index(index)
        self._scope_cache = None
        self._invalidate_scope_cache()
        self.project_changed.emit()
        self.scope_changed.emit()

    def set_mode(self, mode: EditMode) -> None:
        if self._mode is mode:
            return
        self._mode = mode

    def set_current_time(self, t: float) -> None:
        self._current_time = t
        # 播放头跨作用域才发 scope_changed(避免每帧刷新面板)
        scope = self.current_scope()
        if scope is not self._scope_cache:
            self._scope_cache = scope
            self.scope_changed.emit()

    def set_hover(self, target: str) -> None:
        self._hover_target = target

    # ---------- 补丁/复制规则动作(勾选按播放头所在用户段,与旧版一致) ----------
    def toggle_patch(self, pid: str, on: bool) -> None:
        if self._project is None:
            return
        seg = self.current_user_segment()
        self._project.toggle_patch_in_segment(pid, on, seg)
        self.scope_changed.emit()

    def toggle_copy(self, rid: str, on: bool) -> None:
        if self._project is None:
            return
        seg = self.current_user_segment()
        self._project.toggle_copy_in_segment(rid, on, seg)
        self.scope_changed.emit()

    def add_patch(self, patch) -> None:
        if self._project is None:
            return
        self._project.add_patch_to_context(patch, self.current_user_segment())
        self.scope_changed.emit()

    def add_copy(self, rule) -> None:
        if self._project is None:
            return
        scope = self.current_scope()
        rule.anchor_grid = scope.grid_key if scope is not None else "project"
        self._project.add_copy_to_context(rule, self.current_user_segment())
        self.scope_changed.emit()

    def delete_patch(self, pid: str) -> None:
        if self._project is None:
            return
        self._project.remove_patch(pid)
        self.project_changed.emit()
        self.scope_changed.emit()

    def delete_copy(self, rid: str) -> None:
        if self._project is None:
            return
        self._project.remove_copy_rule(rid)
        self.project_changed.emit()
        self.scope_changed.emit()

    def split_at(self, t: float, fps: float) -> None:
        if self._project is None:
            return
        if self._frame_index is not None:
            # VFR:把播放头时间吸附到最近真实帧 pts,再分割
            frame = self._frame_index.nearest_frame(t)
            t = self._frame_index.seconds(frame)
            fps = getattr(self._video_meta, "fps", fps) or fps
        self._project.split_at(t, fps)
        if self._frame_index is not None:
            self._project.apply_frame_index(self._frame_index)
        self.project_changed.emit()
        self.scope_changed.emit()

    def delete_segment_at(self, index: int) -> None:
        if self._project is not None:
            self._project.remove_segment_merge(index)
            self.project_changed.emit()
            self.scope_changed.emit()

    def set_layout(self, grid) -> None:
        """切布局:新网格只赋给播放头所在段(无段则全局),随后同步裁剪。

        切布局本身不重算补丁/复制的几何数值;但它们的 anchor_grid 会解析到
        新赋值的网格(渲染/额外目标派生/后续编辑均用新网格)。这等价于
        "几何值冻结、解析网格跟随新网格",与旧版"对象引用冻结在旧网格快照"
        不同——用户切布局后应检查或重建该段相关补丁/复制。
        """
        if self._project is None:
            return
        seg = self.current_user_segment()
        if seg is not None:
            seg.grid = grid
        else:
            self._project.grid = grid
        self._project.bump()
        self._sync_crop()
        self.scope_changed.emit()

    def move_inner_edge(self, edge_idx: int, nx: float) -> None:
        """拖内部分界线:先物化段网格(继承 → 克隆)再移动,随后重算锚定补丁。"""
        if self._project is None:
            return
        seg = self.current_user_segment()
        if seg is not None:
            self._project.materialize_segment_grid(seg)
        g = self.current_grid()          # 物化后重新解析(段网格已独立)
        if g is not None:
            g.move_edge(edge_idx, nx)
        grid_key = f"segment:{seg.id}" if seg is not None and seg.grid is not None else "project"
        self._project.realign_patches(grid_key)
        self.scope_changed.emit()

    def set_crop(self, lo: float, hi: float) -> None:
        """边缘线移动 = 裁剪:唯一入口,全网格同步 + 段锚定补丁重算。"""
        if self._project is None:
            return
        self._project.set_crop(lo, hi)
        self._project.realign_patches("project")
        for s in self._project.segments:      # 段物化自有网格时补丁锚定该段
            if s.grid is not None:
                self._project.realign_patches(f"segment:{s.id}")
        self.scope_changed.emit()

    def _sync_crop(self) -> None:
        if self._project is None:
            return
        self._project.set_crop(self._project.grid.crop_left,
                               self._project.grid.crop_right)

    def set_range(self, lo: float, hi: float) -> None:
        if self._project is None or hi <= lo:
            return
        self._project.process_range = [lo, hi]
        self._project.bump()
        if self._frame_index is not None:
            # VFR:起止吸附到最近真实帧,范围时间用真实 PTS
            self._project.apply_frame_index(self._frame_index)
        else:
            fps = getattr(self._video_meta, "fps", 0.0) or 30.0
            self._project.process_range_frames = [int(round(lo * fps)),
                                                  int(round(hi * fps))]
            self._project.process_range_pts_ticks = None
        self.scope_changed.emit()

    def set_settings(self, settings) -> None:
        if self._project is not None:
            self._project.settings = settings
            self.project_changed.emit()

    def set_queue_jobs(self, jobs: list[Job]) -> None:
        self._queue_jobs = list(jobs)
        self.queue_changed.emit()
