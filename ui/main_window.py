"""MainWindow:UI 组装 + 信号接线 + 服务薄包装(旧版 1269 行 → 当前约 690 行)。

工作流逻辑 → AppState;键分发 → KeyRouter;音频 → AudioOutput;
队列 UI → QueueWidget;文件/工程/模板/预览/批量 → main_window_services;
补丁/复制编辑命令 → EditController;悬停上报 → HoverTracker。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPushButton, QSlider,
                               QSplitter, QToolBar, QVBoxLayout, QWidget)

from core.grid import GridLayout
from services.audio_output import AudioOutput
from services.decoder import DecodeWorker
from services.frame_index import FrameIndexer
from services.player_controller import PlayerController
from services.render_controller import RenderController
from state.app_state import AppState, EditMode, Stage
from .edit_controller import EditController
from .frame_view import FrameView
from .hover_tracker import HoverTracker
from .key_bindings import KeyRouter
from .patch_panel import PatchPanel
from .queue_widget import QueueWidget
from .style import QSS
from .timeline_widget import TimelineWidget
from .video_info_panel import VideoInfoPanel
from .widgets import PaddedSpin, fmt_hmsf, show_error
from . import main_window_services as mw_services

_LAYOUT_FACTORIES = {
    "thirds": GridLayout.thirds,
    "halves": GridLayout.halves,
    "quarters": GridLayout.quarters,
    "none": GridLayout,
}


class MainWindow(QMainWindow):
    """主窗口:UI 组装 + 信号接线。"""

    def __init__(self, settings_org: str = "FenshenFu",
                 settings_app: str = "fenshenfu", parent=None):
        super().__init__(parent)
        self.setWindowTitle("分身斧修复工具")
        self.setStyleSheet(QSS)
        self._state = AppState(settings_org, settings_app, self)
        self._worker = DecodeWorker(self)
        self._worker.start()        # 解码线程常驻(命令队列驱动,空闲自睡)
        self._player = PlayerController(self._worker, self)
        self._audio = AudioOutput(self)
        self._controller = RenderController(self)
        self._edits = EditController(self)
        self._hover = HoverTracker(self._state, self)
        self._indexer = None
        self._build_ui()
        self._connect()
        self._build_shortcuts()
        self._router = KeyRouter(self)
        self._router.install(self._app(), self._state, self._frame_view,
                             self._on_volume_delta)
        # 记忆恢复
        self._volume_slider.setValue(self._state.volume())
        sizes = self._state.get_setting("splitter_sizes", "")
        if sizes:
            try:
                self._outer_splitter.setSizes([int(x) for x in sizes.split(",")])
            except ValueError:
                pass
        self.resize(1200, 800)
        win_size = self._state.get_setting("window_size", "")
        if win_size:
            try:
                w, h = (int(x) for x in win_size.split(","))
                if w > 400 and h > 300:
                    self.resize(w, h)
            except ValueError:
                pass
        # 状态栏 100ms 聚合(播放时不每帧 setText 抖动)
        self._frame_count = 0
        self._fps_count_at = 0
        self._fps_t0 = 0.0
        self._batch_results: list = []   # (job_id, ok, msg):queueFinished 统一汇总
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(100)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start()

    # ================= UI 组装 =================
    def _build_ui(self) -> None:
        # 工具栏(与 V1 一致):打开视频/保存工程/打开工程;快捷键由 QAction
        # 承担(替代旧 QShortcut,避免重复注册同一键位)
        tb = QToolBar("主工具栏")
        tb.setMovable(False)
        self.addToolBar(tb)
        for text, key, slot in (
                ("打开视频 (Ctrl+O)", "Ctrl+O", self._open_video_dialog),
                ("保存工程 (Ctrl+S)", "Ctrl+S", self._save_project),
                ("打开工程 (Ctrl+Shift+O)", "Ctrl+Shift+O",
                 self._open_project_dialog)):
            a = QAction(text, self)
            a.setShortcut(QKeySequence(key))
            a.triggered.connect(slot)
            tb.addAction(a)
        self._info = VideoInfoPanel()
        self._frame_view = FrameView()
        self._frame_view.set_state(self._state)
        self._timeline = TimelineWidget()
        self._patch_panel = PatchPanel()
        self._queue_widget = QueueWidget()

        mid = QWidget()
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.addWidget(self._frame_view, 1)
        ml.addWidget(self._build_transport())
        ml.addWidget(self._timeline)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._info)
        splitter.addWidget(mid)
        splitter.addWidget(self._patch_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([280, 880, 460])
        self._patch_panel.setMinimumWidth(380)

        outer = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(splitter)
        outer.addWidget(self._queue_widget)
        outer.setSizes([600, 300])
        self._queue_widget.setMinimumHeight(200)
        self._outer_splitter = outer
        self.setCentralWidget(outer)

        # 状态栏 label(fps/鼠标坐标/音频诊断/工作流)
        self._fps_label = QLabel("")
        self._pos_label = QLabel("")
        self._audio_state_label = QLabel("")
        self._state_label = QLabel("")
        sb = self.statusBar()
        for w in (self._fps_label, self._pos_label,
                  self._audio_state_label, self._state_label):
            sb.addPermanentWidget(w)
        sb.showMessage("打开视频开始（Ctrl+O）")

    def _build_transport(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        # 第一行:播放/音量/时间跳转
        row1 = QHBoxLayout()
        self._btn_play = QPushButton("播放")
        self._btn_play.setToolTip("空格")
        self._btn_play.clicked.connect(self._player.toggle)
        row1.addWidget(self._btn_play)
        row1.addWidget(QLabel("音量"))
        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setFixedWidth(80)
        self._volume_slider.setValue(70)
        row1.addWidget(self._volume_slider)
        # 当前播放时间 / 视频总时长(与跳转框分离:跳转框只作输入,不回写)
        self._time_label = QLabel("00:00:00:00")
        self._time_label.setStyleSheet("font-family: monospace;")
        row1.addWidget(self._time_label)
        row1.addWidget(QLabel("/"))
        self._duration_label = QLabel("00:00:00:00")
        self._duration_label.setStyleSheet("font-family: monospace;")
        row1.addWidget(self._duration_label)
        self._jump_h = PaddedSpin(99)
        self._jump_m = PaddedSpin(59)
        self._jump_s = PaddedSpin(59)
        self._jump_f = PaddedSpin(60)
        for sp in (self._jump_h, self._jump_m, self._jump_s, self._jump_f):
            row1.addWidget(sp)
        lbl_f = QLabel("帧")
        lbl_f.setToolTip("帧号从 1 开始")
        row1.addWidget(lbl_f)
        btn_jump = QPushButton("跳转")
        btn_jump.clicked.connect(self._on_jump)
        row1.addWidget(btn_jump)
        btn_prev = QPushButton("上一帧")
        btn_prev.setToolTip("暂停并后退 1 帧")
        btn_prev.clicked.connect(lambda: self._player.step(-1))
        btn_next = QPushButton("下一帧")
        btn_next.setToolTip("暂停并前进 1 帧")
        btn_next.clicked.connect(lambda: self._player.step(1))
        row1.addWidget(btn_prev)
        row1.addWidget(btn_next)
        row1.addStretch(1)   # 行尾 stretch:防剩余空间平均拉宽所有控件
        lay.addLayout(row1)
        # 第二行:分割按钮 + 模式按钮组(网格模式已并入选择)
        row2 = QHBoxLayout()
        self._btn_split = QPushButton("分割")
        self._btn_split.setToolTip("把当前时间所在的片段一分为二,并跳转片段页")
        self._btn_split.clicked.connect(self._on_split_button)
        row2.addWidget(self._btn_split)
        self._mode_btns = {}
        for key, text, tip in (("select", "选择", "选择/移动/缩放补丁,拖动分界线"),
                               ("target", "目标", "画脏区域(红)"),
                               ("source", "源", "画干净区域(绿)")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.clicked.connect(lambda _, k=key: self._set_mode(k))
            row2.addWidget(b)
            self._mode_btns[key] = b
        self._btn_copy = QPushButton("复制整格")
        self._btn_copy.setToolTip("添加复制规则并跳转复制页")
        self._btn_copy.clicked.connect(self._on_copy_add)
        row2.addWidget(self._btn_copy)
        self._btn_preview = QPushButton("预览当前帧")
        self._btn_preview.clicked.connect(self._preview_frame)
        row2.addWidget(self._btn_preview)
        # 播放倍速(0.5/1/2/3x;新打开视频默认 1 倍;音调随倍速变化)
        self._speed_group = QButtonGroup(self)
        self._speed_btns = {}
        for text, r in (("0.5倍", 0.5), ("1倍", 1.0), ("2倍", 2.0), ("3倍", 3.0),
                        ("5倍", 5.0)):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip("播放倍速(音调随倍速变化)")
            b.clicked.connect(lambda _, rr=r: self._on_speed(rr))
            self._speed_group.addButton(b)
            self._speed_btns[r] = b
            row2.addWidget(b)
        self._speed_btns[1.0].setChecked(True)
        # 模板(补丁/复制/边缘线/分界线):保存到软件目录,应用到当前段或全局
        for text, slot in (("保存模板", self._save_template),
                           ("应用模板", self._apply_template)):
            b = QPushButton(text)
            b.setToolTip("把补丁/复制/边缘线/分界线保存为模板,存软件目录 templates/"
                         if text == "保存模板" else
                         "把模板应用到当前时间所在分段(全局模式 = 应用到全局,替换现有内容)")
            b.clicked.connect(slot)
            row2.addWidget(b)
        row2.addStretch(1)
        lay.addLayout(row2)
        return w

    def _on_split_button(self) -> None:
        """分割按钮:分割 + 自动跳转片段页。"""
        self._on_split(self._player.current_time)
        self._patch_panel.switch_tab("segments")

    # ================= 信号接线 =================
    def _connect(self) -> None:
        s = self._state
        s.project_changed.connect(self._refresh_all)
        s.scope_changed.connect(self._refresh_scope)
        s.stage_changed.connect(self._on_stage_changed)
        # player/worker
        self._player.opened.connect(self._on_opened)
        self._player.playingChanged.connect(self._on_playing_changed)
        self._player.frameReady.connect(self._on_frame)
        self._player.timeChanged.connect(self._on_time_changed)
        self._player.errorOccurred.connect(
            lambda msg: show_error(self, "解码错误", msg))
        self._worker.audioData.connect(self._audio.write)
        self._worker.audioReset.connect(self._audio.reset)
        self._audio.state_changed.connect(self._on_audio_state)
        # frame_view
        fv = self._frame_view
        fv.patchCreated.connect(self._on_patch_created)
        fv.patchChanged.connect(lambda p: fv.refresh())
        fv.edgeMoveBlocked.connect(self._on_edge_move_blocked)
        fv.mousePosChanged.connect(self._on_mouse_pos)
        # timeline(分割仅走工具栏"分割"按钮,双击时间轴已移除)
        self._timeline.seekRequested.connect(self._on_timeline_seek)
        # info panel
        self._info.openRequested.connect(self._open_video_dialog)
        self._info.layoutChanged.connect(self._on_layout_changed)
        self._info.rangeChanged.connect(s.set_range)
        self._info.settingsChanged.connect(self._on_settings_changed)
        self._info.useCurrentForStart.connect(
            lambda: self._info._set_range_notify(self._player.current_time,
                                                 self._info.get_range()[1]))
        self._info.useCurrentForEnd.connect(
            lambda: self._info._set_range_notify(self._info.get_range()[0],
                                                 self._player.current_time))
        # patch panel
        pp = self._patch_panel
        pp.patchToggled.connect(self._on_patch_toggled)
        pp.patchSourceChanged.connect(self._on_patch_source_changed)
        pp.patchTargetsChanged.connect(self._on_patch_targets_changed)
        pp.patchLockChanged.connect(self._on_patch_lock_changed)
        pp.patchDeleted.connect(self._on_patch_deleted)
        pp.copyToggled.connect(self._on_copy_toggled)
        pp.copySourceChanged.connect(self._on_copy_source_changed)
        pp.copyTargetsChanged.connect(self._on_copy_targets_changed)
        pp.copyFlipChanged.connect(self._on_copy_flip_changed)
        pp.copyDeleted.connect(self._on_copy_deleted)
        pp.segmentDeleted.connect(s.delete_segment_at)
        # queue
        q = self._queue_widget
        q.addProjectRequested.connect(self._queue_add_current)
        q.batchImportRequested.connect(self._on_batch_import)
        q.startQueueRequested.connect(self._controller.start_queue)
        q.cancelCurrentRequested.connect(self._controller.cancel_current)
        q.cancelAllRequested.connect(self._controller.cancel_all)
        q.removeSelectedRequested.connect(self._queue_remove_selected)
        c = self._controller
        c.jobStateChanged.connect(self._on_job_state)
        c.jobProgress.connect(self._on_job_progress)
        c.jobFinished.connect(self._on_job_finished)
        c.logLine.connect(self._queue_widget.append_log)
        c.queueFinished.connect(self._on_queue_finished)
        c.retryPrompt.connect(self._on_retry_prompt)
        # 队列 UI 刷新(修复:此前 queue_changed 未接线,添加当前工程无显示)
        s.queue_changed.connect(self._on_queue_changed)
        # volume
        self._volume_slider.valueChanged.connect(self._on_volume_changed)
        self._hover.track(self._volume_slider, "volume")

    def _build_shortcuts(self) -> None:
        self._seek_shortcuts = {}   # 显式保活(QShortcut 的 Python 包装防 GC)
        for mod, secs in ((Qt.KeyboardModifier.NoModifier, 1),
                          (Qt.KeyboardModifier.ControlModifier, 15),
                          (Qt.KeyboardModifier.ShiftModifier, 30)):
            for key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                sc = QShortcut(QKeySequence(mod.value | key.value), self)
                sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
                sc.activated.connect(lambda k=key, s=secs: self._seek_arrow(k, s))
                self._seek_shortcuts[(mod, key)] = sc
        sc = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.activated.connect(self._player.toggle)
        # 文件快捷键由工具栏 QAction 承担(_build_ui,避免重复注册)

    def _app(self):
        from PySide6.QtWidgets import QApplication
        return QApplication.instance()

    # ================= 工作流 =================
    def _on_opened(self, info: dict) -> None:
        self._speed_btns[1.0].setChecked(True)   # 新打开视频默认 1 倍速(worker 已重置)
        self._fps_label.setText(f"{info.get('fps', 0):.2f} fps")
        dur = info.get("duration", 0.0)
        self._duration_label.setText(fmt_hmsf(dur, info.get("fps", 30.0)))
        self._jump_f.setMaximum(max(1, int(round(info.get("fps", 30.0)))))
        audio = info.get("audio")
        if audio:
            self._audio.open(audio["sample_rate"], audio["channels"])
        else:
            self._audio_state_label.setText("无音轨")

    def _on_playing_changed(self, on: bool) -> None:
        self._btn_play.setText("暂停" if on else "播放")   # 纯文字,不用 emoji
        self._audio.playing(on)

    def _on_frame(self, img, pts: float) -> None:
        self._frame_count += 1
        self._frame_view.set_frame(img, pts)

    def _on_time_changed(self, t: float) -> None:
        self._state.set_current_time(t)
        self._timeline.set_playhead(t)

    def _update_status(self) -> None:
        """100ms 聚合:播放帧率(计数率)+ 当前播放时间显示(不每帧 setText)。"""
        import time as _time
        now = _time.perf_counter()
        if self._fps_t0 and now - self._fps_t0 >= 0.5:
            fps = (self._frame_count - self._fps_count_at) / (now - self._fps_t0)
            self._fps_label.setText(f"{fps:.2f} fps")
            self._fps_count_at = self._frame_count
            self._fps_t0 = now
        elif not self._fps_t0:
            self._fps_t0 = now
            self._fps_count_at = self._frame_count
        self._time_label.setText(fmt_hmsf(self._player.current_time, self._player.fps))

    def _on_audio_state(self, state) -> None:
        kind, msg = state
        if kind == "ok":
            self._audio_state_label.setText("")
        elif kind == "no_device":
            self._audio_state_label.setText("预览无声音(无输出设备)")
            self._audio_state_label.setStyleSheet("color:#e67e22;")
        else:
            self._audio_state_label.setText("预览无声音")
            self._audio_state_label.setStyleSheet("color:#e67e22;")
            if msg:
                self._audio_state_label.setToolTip(msg)

    def _on_jump(self) -> None:
        fps = self._player.fps or 30.0
        t = (self._jump_h.value() * 3600 + self._jump_m.value() * 60
             + self._jump_s.value() + (self._jump_f.value() - 1) / fps)
        self._player.seek(t)

    def _on_timeline_seek(self, t: float) -> None:
        if abs(t - self._player.current_time) > 0.01:   # 防抖
            self._player.seek(t)

    def _on_split(self, t: float) -> None:
        self._state.split_at(t, self._player.fps)

    # ---- 刷新 ----
    def _refresh_all(self) -> None:
        self._refresh_scope()
        self._patch_panel.set_project(self._state.project)
        self._frame_view.refresh()

    def _refresh_scope(self) -> None:
        s = self._state
        proj = s.project
        if proj is None:
            self._timeline.set_data(0, (0, 0), [], 0)
            self._patch_panel.set_project(None)
            return
        scope = s.current_scope()
        self._frame_view.set_scope_context(scope)
        self._patch_panel.set_scope_context(scope)
        self._patch_panel.refresh_segments()   # 补丁数实时(增删启停都经 scope_changed)
        self._timeline.set_data(proj.duration, proj.process_range,
                                s.effective_scopes(),
                                s.current_time, proj.segments)
        self._frame_view.set_copy_highlight(self._copy_highlight())
        # 布局下拉跟随当前 scope(blockSignals 防循环)
        g = scope.grid if scope is not None else None
        n = len(g.tiles) if g is not None else 0
        key = ("thirds" if n == 3 else ("halves" if n == 2
                                        else ("quarters" if n == 4 else "none")))
        self._info.set_layout_key(key)

    def _copy_highlight(self) -> list:
        """当前 scope 启用的复制规则 → [(来源格, [目标格], flip_horizontal), ...]。

        格索引按规则自己的 anchor_grid 解析(与渲染同源)。
        """
        s = self._state
        proj = s.project
        scope = s.current_scope()
        if proj is None or scope is None:
            return []
        out = []
        for rid in scope.copy_rule_ids:
            r = proj.copy_rule(rid)
            if r is None or not r.target_tile_indices:
                continue
            g = proj.resolve_grid(r.anchor_grid)
            if not g.tiles:
                continue
            out.append((r.source_tile_idx,
                        [t for t in r.target_tile_indices if t < len(g.tiles)],
                        r.flip_horizontal, r.anchor_grid))
        return out

    def _on_stage_changed(self) -> None:
        if self._state.stage is Stage.NO_VIDEO:
            self._state_label.setText("")
        else:
            self._state_label.setText("已打开视频")

    # ---- 补丁/复制(薄包装 → EditController) ----
    def _on_patch_created(self, patch) -> None:
        self._edits.patch_created(patch)

    @staticmethod
    def _tile_label(idx: int) -> str:
        return EditController.tile_label(idx)

    def _on_patch_toggled(self, pid: str, on: bool) -> None:
        self._edits.patch_toggled(pid, on)

    def _on_patch_source_changed(self, pid: str, tile_idx: int) -> None:
        self._edits.patch_source_changed(pid, tile_idx)

    def _on_patch_targets_changed(self, pid: str, main_idx: int, extra: list) -> None:
        self._edits.patch_targets_changed(pid, main_idx, extra)

    def _on_patch_lock_changed(self, pid: str, locked: bool) -> None:
        self._edits.patch_lock_changed(pid, locked)

    def _on_patch_deleted(self, pid: str) -> None:
        self._edits.patch_deleted(pid)

    def _on_copy_add(self) -> None:
        self._edits.copy_add()

    def _on_copy_toggled(self, rid: str, on: bool) -> None:
        self._edits.copy_toggled(rid, on)

    def _on_copy_source_changed(self, rid: str, idx: int) -> None:
        self._edits.copy_source_changed(rid, idx)

    def _on_copy_targets_changed(self, rid: str, indices: list) -> None:
        self._edits.copy_targets_changed(rid, indices)

    def _on_copy_flip_changed(self, rid: str, on: bool) -> None:
        self._edits.copy_flip_changed(rid, on)

    def _on_copy_deleted(self, rid: str) -> None:
        self._edits.copy_deleted(rid)

    # ---- 布局/设置 ----
    def _on_layout_changed(self, key: str) -> None:
        factory = _LAYOUT_FACTORIES.get(key, GridLayout)
        self._state.set_layout(factory())

    def _on_settings_changed(self) -> None:
        if self._state.project is not None:
            self._state.set_settings(self._info.get_settings())

    def _show_error(self, title: str, msg: str) -> None:
        show_error(self, title, msg)

    def _on_edge_move_blocked(self) -> None:
        # 用户明确要求文案
        QMessageBox.information(self, "无法移动边界",
                                "移动边界会导致现有补丁/复制对齐错位，请删除补丁/复制后重试")

    def _on_mouse_pos(self, nx: float, ny: float) -> None:
        self._pos_label.setText(f"x {nx:.1%}  y {ny:.1%}")

    def _on_volume_changed(self, v: int) -> None:
        self._state.set_volume(v)
        self._audio.set_volume(v)

    def _on_volume_delta(self, delta: int) -> None:
        self._volume_slider.setValue(max(0, min(100, self._volume_slider.value() + delta)))

    # ---- 文件/工程/模板(薄包装 → main_window_services) ----
    def _open_video_dialog(self) -> None:
        mw_services.open_video_dialog(self)

    def _load_video(self, path: str) -> None:
        mw_services.load_video(self, path)

    def _start_frame_indexer(self, path: str) -> None:
        """打开视频后后台建立真实帧索引(VFR/规则 timebase 边界精确)。"""
        if self._indexer is not None and self._indexer.isRunning():
            self._indexer.wait(2000)
        self._indexer = FrameIndexer(path, self)
        self._indexer.indexReady.connect(self._on_frame_index_ready)
        self._indexer.indexFailed.connect(self._on_frame_index_failed)
        self._indexer.start()

    def _on_frame_index_ready(self, index) -> None:
        self._state.set_frame_index(index)
        if self._state.project is not None:
            self._info.set_range(*self._state.project.process_range)
        self._state_label.setText("帧索引已就绪")

    def _on_frame_index_failed(self, msg: str) -> None:
        self._state_label.setText(f"帧索引建立失败:{msg}")

    def _save_project(self) -> None:
        mw_services.save_project(self)

    def _save_template(self) -> None:
        mw_services.save_template_action(self)

    def _apply_template(self) -> None:
        mw_services.apply_template_action(self)

    def _open_project_dialog(self) -> None:
        mw_services.open_project_dialog(self)

    # ---- 预览/对齐(薄包装 → main_window_services) ----
    def _preview_frame(self) -> None:
        mw_services.preview_frame(self)

    def _show_preview_dialog(self, orig_path: str, fixed_path: str) -> None:
        mw_services.show_preview_dialog(self, orig_path, fixed_path)

    @staticmethod
    def _preview_label(pm, title: str):
        return mw_services.preview_label(pm, title)

    # ---- 队列(薄包装 → main_window_services) ----
    def _queue_add_current(self) -> None:
        mw_services.queue_add_current(self)

    def _on_batch_import(self) -> None:
        mw_services.batch_import(self)

    def _queue_remove_selected(self) -> None:
        jid = self._queue_widget.selected_row_job()
        if jid is None:
            self._state_label.setText("请先选中要移除的任务")
            return
        if not self._controller.remove_job(jid):
            self._state_label.setText("运行中的任务不可移除")   # 不静默
            return
        self._state.set_queue_jobs(self._controller.jobs())

    def _on_queue_changed(self) -> None:
        """队列快照 → 队列 UI(状态机事件驱动刷新)。"""
        cur = self._controller.current_job
        self._queue_widget.set_jobs(self._state.queue_jobs,
                                    current_job_id=cur.id if cur is not None else None)

    def _on_job_state(self, job_id: str, status: str) -> None:
        self._state.set_queue_jobs(self._controller.jobs())
        self._state_label.setText(f"任务 {job_id}: {status}")

    def _on_job_progress(self, job_id: str, p: float) -> None:
        self._state_label.setText(f"任务 {job_id}: {p * 100:.0f}%")
        self._state.set_queue_jobs(self._controller.jobs())   # 进度条实时刷新

    def _on_job_finished(self, job_id: str, ok: bool, msg: str) -> None:
        """任务完成:记录结果,不逐任务弹窗(queueFinished 统一汇总)。"""
        self._batch_results.append((job_id, ok, msg))

    def _on_queue_finished(self) -> None:
        """全部排队任务处理完 → 同一个窗口统一提醒(成功 N 个 / 失败列表)。"""
        self._state_label.setText("队列处理完毕")
        if not self._batch_results:
            return
        ok_n = sum(1 for _, ok, _ in self._batch_results if ok)
        fails = [(jid, msg) for jid, ok, msg in self._batch_results if not ok]
        if not fails:
            QMessageBox.information(self, "处理完成",
                                    f"全部 {ok_n} 个任务处理完成")
        else:
            lines = "\n".join(f"• {jid}:{msg[:80]}" for jid, msg in fails[:6])
            if len(fails) > 6:
                lines += f"\n…等共 {len(fails)} 个失败"
            QMessageBox.warning(self, "处理完成",
                                f"完成 {ok_n} 个,失败 {len(fails)} 个:\n{lines}")
        self._batch_results = []

    def _on_retry_prompt(self, job_id: str) -> None:
        """NVENC 失败三选一(不自动切换/不静默兜底)。"""
        ret = QMessageBox.question(
            self, "NVENC 编码失败",
            f"任务 {job_id} 的 NVENC 编码失败。\n\n"
            "是否改用软件编码重试?\n(选\"取消\"将跳过当前任务并取消全部)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes)
        if ret == QMessageBox.StandardButton.Yes:
            self._controller.retry_software()
        elif ret == QMessageBox.StandardButton.No:
            self._controller.skip_job()
        else:
            self._controller.skip_job()
            self._controller.cancel_all()

    # ---- 模式/快捷键 ----
    def _on_speed(self, r: float) -> None:
        """播放倍速切换:worker 节流 + 音频样本变速同步。"""
        self._player.set_rate(r)
        for rr, b in self._speed_btns.items():
            b.setChecked(rr == r)

    def _set_mode(self, key: str) -> None:
        mapping = {"select": EditMode.SELECT, "target": EditMode.DST,
                   "source": EditMode.SRC}
        mode = mapping.get(key)
        if mode is None:
            return
        self._state.set_mode(mode)
        self._frame_view.set_mode(key)
        for k, b in self._mode_btns.items():
            b.setChecked(k == key)
        if key in ("target", "source"):
            self._patch_panel.switch_tab("patch")   # 目标/源模式自动跳补丁页

    def _seek_arrow(self, key, secs: float) -> None:
        t = self._player.current_time + (secs if key == Qt.Key.Key_Right else -secs)
        t = max(0.0, min(self._player.duration, t))
        self._player.seek(t)

    def keyPressEvent(self, e) -> None:
        key = e.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            # 兜底秒跳(KeyRouter/QShortcut 未消费时)
            mods = e.modifiers()
            secs = 30 if (mods & Qt.KeyboardModifier.ShiftModifier) else \
                15 if (mods & Qt.KeyboardModifier.ControlModifier) else 1
            self._seek_arrow(key, secs)
        else:
            super().keyPressEvent(e)

    # ================= 关闭 =================
    def closeEvent(self, e) -> None:
        sizes = ",".join(str(x) for x in self._outer_splitter.sizes())
        self._state.set_setting("splitter_sizes", sizes)
        self._state.set_setting("window_size",
                                f"{self.width()},{self.height()}")
        self._status_timer.stop()
        self._router.uninstall(self._app())
        if self._indexer is not None and self._indexer.isRunning():
            self._indexer.wait(2000)
        self._worker.stop()          # 含 wait(5000)
        self._audio.close()
        self._controller.cancel_all()
        super().closeEvent(e)
