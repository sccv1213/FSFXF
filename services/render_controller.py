"""渲染控制器:QProcess 状态机,串行执行队列任务。

每任务流程:逐段编码(-progress pipe:1 解析进度)→ concat 拼接 →
原子改名输出 → 参数核验 → 下一任务。NVENC 失败时暂停并询问用户
(软件重试 / 跳过 / 取消全部),不自动切换。

状态机设计(重写核心):RenderState 是唯一真源,`busy` 等派生——
旧版 `_running`/`_cur` 双字段 + 每个退出路径手动复位的卡死 bug
(skip_job 漏复位 → 队列永久卡死)在结构上不可能:
每个转移是单一赋值,`_advance()` 只从 IDLE 取下一个任务。
NVENC_PENDING 期间不推进队列(等待用户三选一)。

可测性:process_factory 注入假进程(测试脚本化 emit finished/errorOccurred,
穷举转移表,不依赖真实 ffmpeg)。
"""
from __future__ import annotations

import copy
import os
import shutil
import tempfile
from dataclasses import replace
from enum import Enum

from PySide6.QtCore import QObject, QProcess, Signal

from core.ffprobe import MediaInfo, find_ffmpeg, probe
from core.planning import Scope, effective_scopes
from core.project import Project
from core.render_commands import (build_audio_only_cmd, build_concat_cmd,
                                    build_mux_audio_file_cmd,
                                    build_mux_original_audio_cmd,
                                    build_segment_cmd, build_vfr_fix_cmd,
                                    overlay_ops, verify_output)
from .frame_index import build_frame_index
from .batch_queue import Job, JobQueue, JobStatus

_LOG_TAIL = 30


class RenderState(Enum):
    """控制器状态(唯一真源;任务级状态在 Job.status)。"""
    IDLE = 0            # 无进程,不等待用户
    RUNNING = 1         # 有进程在跑(逐段/拼接/核验)
    NVENC_PENDING = 2   # NVENC 失败,等待用户三选一;队列不自动推进

    @property
    def busy(self) -> bool:
        return self in (RenderState.RUNNING, RenderState.NVENC_PENDING)


class RenderController(QObject):
    jobProgress = Signal(str, float)           # job_id, 0..1
    jobStateChanged = Signal(str, str)         # job_id, status.value
    jobFinished = Signal(str, bool, str)       # job_id, ok, message
    logLine = Signal(str)
    queueFinished = Signal()
    retryPrompt = Signal(str)                  # job_id(NVENC 失败,等待用户决定)

    def __init__(self, parent=None, process_factory=None, ffmpeg_path=None):
        super().__init__(parent)
        self._factory = process_factory or (lambda p: QProcess(p))
        self._proc = self._factory(self)
        self._proc.readyReadStandardOutput.connect(self._on_stdout)
        self._proc.readyReadStandardError.connect(self._on_stderr)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_proc_error)   # 启动失败也要报错,不能卡死
        self._queue = JobQueue()
        self._state = RenderState.IDLE
        self._cur: Job | None = None
        # 当前任务内部状态
        self._stages: list[list[str]] = []
        self._stage_kinds: list[str] = []
        self._stage_outputs: list[str] = []
        self._stage_idx = 0
        self._stage_kind = ""
        self._tmpdir = ""
        self._final_out = ""
        self._final_source = ""
        self._seg_durs: list[float] = []
        self._done_dur = 0.0
        self._total_dur = 0.0
        self._expected_frames = None
        self._cancelled = False
        self._nvenc = False
        self._media: MediaInfo | None = None
        self._stderr_tail: list[str] = []
        self._ffmpeg = ffmpeg_path or find_ffmpeg()

    # ---- 派生查询(UI 只问这里,无独立标志位) ----
    @property
    def state(self) -> RenderState:
        return self._state

    @property
    def current_job(self) -> Job | None:
        return self._cur

    def jobs(self) -> list[Job]:
        return self._queue.all()

    # ---------- 队列操作 ----------
    def submit(self, project: Project, auto_start: bool = True) -> str:
        # 入队深拷贝:入队后继续编辑工程不影响已排队任务
        snapshot = Project.from_dict(project.to_dict())
        snapshot.frame_index = getattr(project, "frame_index", None)
        job = self._queue.add(snapshot)
        self.jobStateChanged.emit(job.id, JobStatus.QUEUED.value)
        if auto_start:
            self._advance()
        return job.id

    def start_queue(self) -> None:
        """开始处理:被取消的任务重置为排队(可再次处理)。"""
        for j in self._queue.all():
            if j.status is JobStatus.CANCELLED:
                self.jobStateChanged.emit(j.id, JobStatus.QUEUED.value)
        self._queue.reset_cancelled()
        self._advance()

    def remove_job(self, job_id: str) -> bool:
        """移除任务(排队中/已完成可移除;运行中返回 False)。"""
        if self._queue.remove(job_id):
            self.jobStateChanged.emit(job_id, JobStatus.CANCELLED.value)
            return True
        return False

    def cancel_current(self) -> None:
        if self._proc.state() != QProcess.ProcessState.NotRunning:
            self._cancelled = True
            self._proc.kill()

    def cancel_all(self) -> None:
        """取消全部:任务保留在队列(标记 CANCELLED),之后可再次开始处理。"""
        self.cancel_current()
        for j in self._queue.all():
            if j.status is JobStatus.QUEUED:
                self.jobStateChanged.emit(j.id, JobStatus.CANCELLED.value)
        self._queue.mark_cancelled_all()

    def retry_software(self) -> None:
        """NVENC 失败 → 用户选择软件重试:重建当前任务全部命令。

        只改当前任务用的 settings 副本,不动用户活工程(避免重试后保存
        工程时悄悄带上 sw 设置)。
        """
        if self._state is not RenderState.NVENC_PENDING or self._cur is None:
            return
        self._cur.status = JobStatus.RUNNING
        self.jobStateChanged.emit(self._cur.id, JobStatus.RUNNING.value)
        proj = copy.copy(self._cur.project)                  # 浅拷贝:settings 独立
        proj.settings = replace(proj.settings, encoder_mode="sw")
        self._cur.project = proj
        self._state = RenderState.RUNNING
        self._cleanup_tmp()   # 清掉 NVENC 失败那次遗留的临时目录,再重建
        self._begin_job(proj)

    def skip_job(self) -> None:
        """NVENC 失败 → 用户选择跳过。state 单一赋值,漏复位不可能。"""
        if self._state is not RenderState.NVENC_PENDING or self._cur is None:
            return
        self._cur.status = JobStatus.FAILED
        self._cur.message = "用户选择跳过"
        self._cleanup_tmp()
        self.jobStateChanged.emit(self._cur.id, JobStatus.FAILED.value)
        self.jobFinished.emit(self._cur.id, False, self._cur.message)
        self._cur = None
        self._state = RenderState.IDLE
        self._advance()

    # ---------- 任务流程 ----------
    def _advance(self) -> None:
        """从终态推进队列:取下一个 queued 任务开始执行。

        仅 IDLE 可推进;NVENC_PENDING 期间调用方(用户)未决定,队列保持。
        """
        if self._state is not RenderState.IDLE:
            return
        job = self._queue.next_queued()
        if job is None:
            if self._cur is None:
                self.queueFinished.emit()
            return
        self._cur = job
        job.status = JobStatus.RUNNING
        self.jobStateChanged.emit(job.id, JobStatus.RUNNING.value)
        self._state = RenderState.RUNNING
        self._begin_job(job.project)

    @staticmethod
    def _merge_render_scopes(project: Project, scopes: list[Scope],
                             fw: int, fh: int) -> list[tuple[Scope, list]]:
        """合并时间连续、补丁/复制集合相同、展开 op 完全相同的相邻 scope。

        gap 与无工作段、或布局/启用集合完全一致的相邻段,合成为一个
        编码段,减少 ffmpeg 进程与 concat 拼接点。仅渲染层合并,不修改工程。
        """
        out: list[tuple[Scope, list]] = []
        for s in scopes:
            pxs = overlay_ops(project, s, fw, fh)
            if out and abs(out[-1][0].end - s.start) < 1e-9:
                prev, prev_pxs = out[-1]
                if (prev.patch_ids == s.patch_ids
                        and prev.copy_rule_ids == s.copy_rule_ids
                        and prev_pxs == pxs):
                    merged = Scope(prev.key, prev.start, s.end,
                                   prev.grid, prev.grid_key,
                                   prev.patch_ids, prev.copy_rule_ids,
                                   prev.start_frame, s.end_frame)
                    out[-1] = (merged, prev_pxs)
                    continue
            out.append((s, pxs))
        return out

    def _begin_job(self, proj: Project) -> None:
        """构建全部命令并开始逐段执行。

        音频策略:
        - 处理范围 = 全片:只分段编码视频,最后把整条原音轨流复制合入
          (零损失,且不产生逐段 AAC 包边界漂移)
        - 处理范围 ≠ 全片:单独把整个范围音频重编码一次,再与视频合入
          (避免每段音频边界不齐导致 concat 时长/fps 漂移)
        """
        try:
            media = probe(proj.video_path)
            if not media.width:
                raise RuntimeError(media.warnings[0] if media.warnings else "无法读取视频")
            if media.frame_count <= 0:
                media.frame_count = int(round(media.duration * media.fps))
            if proj.process_range_frames is None:
                lo_f = int(round(proj.process_range[0] * media.fps))
                hi_f = int(round(proj.process_range[1] * media.fps))
                proj.process_range_frames = [lo_f, hi_f]
            scopes = effective_scopes(proj, media.fps)
            if not scopes:
                raise RuntimeError("没有可处理的片段(处理范围为空?)")
            fw, fh = media.width, media.height
            render_scopes = self._merge_render_scopes(proj, scopes, fw, fh)
            if len(render_scopes) < len(scopes):
                self.logLine.emit(f"相邻同 op scope 合并:{len(scopes)} → {len(render_scopes)} 段")
            scopes = [s for s, _ in render_scopes]
            timescale = None
            if media.is_vfr:
                if getattr(proj, "frame_index", None) is None:
                    idx = build_frame_index(proj.video_path)
                    proj.frame_index = idx
                    if proj.process_range_pts_ticks is None:
                        proj.apply_frame_index(idx)
                        scopes = effective_scopes(proj, media.fps)
                        # 帧索引改写边界后,必须重新做 scope 合并与期望帧数
                        render_scopes = self._merge_render_scopes(proj, scopes, fw, fh)
                        scopes = [s for s, _ in render_scopes]
                if proj.video_time_base is not None:
                    num, den = proj.video_time_base
                    timescale = den // num if den % num == 0 else den
            if all(s.frame_count is not None for s in scopes):
                self._expected_frames = sum(s.frame_count for s in scopes)
            else:
                self._expected_frames = None
            total = sum(max(0.0, s.end - s.start) for s in scopes)
            if total <= 0:
                raise RuntimeError("处理范围为空")
            out_dir = proj.settings.output_dir or os.path.dirname(os.path.abspath(proj.video_path))
            os.makedirs(out_dir, exist_ok=True)
            self._tmpdir = tempfile.mkdtemp(dir=out_dir, prefix="fenshenfu_")
            self._final_out = self._dedupe_path(os.path.join(
                out_dir, os.path.splitext(os.path.basename(proj.video_path))[0] + "_修复.mp4"))
            stages: list[list[str]] = []
            kinds: list[str] = []
            outputs: list[str] = []
            durs: list[float] = []

            lo, hi = proj.process_range
            full_range = (abs(lo) < 1e-6
                          and abs(hi - media.duration) < 1e-3)
            if not full_range and proj.process_range_frames is not None and media.frame_count > 0:
                full_range = (proj.process_range_frames[0] == 0
                              and proj.process_range_frames[1] == media.frame_count)
            separate_audio = bool(media.has_audio)

            for i, (scope, pxs) in enumerate(render_scopes):
                crop = None
                # 边缘线全程统一:裁剪边界取 project.grid 首/尾边(所有段网格已同步)
                if proj.grid.crop_left > 0 or proj.grid.crop_right < 1.0:    # 像素级:移动即裁剪
                    left_px = int(round(proj.grid.crop_left * fw))
                    right_px = int(round(proj.grid.crop_right * fw))
                    crop = (left_px, right_px, fh)
                final_seg = os.path.join(self._tmpdir, f"seg_{i:03d}.mp4")
                if media.is_vfr and timescale and scope.start_frame is not None \
                        and scope.end_frame is not None \
                        and getattr(proj, "frame_index", None) is not None:
                    raw_seg = os.path.join(self._tmpdir, f"seg_{i:03d}_raw.mp4")
                    stages.append(build_segment_cmd(
                        self._ffmpeg, media, scope, pxs, proj.settings, proj.video_path,
                        raw_seg, crop=crop, include_audio=not separate_audio,
                        video_timescale=timescale))
                    kinds.append("segment")
                    outputs.append(raw_seg)
                    idx = proj.frame_index
                    sf, ef = scope.start_frame, scope.end_frame
                    last_pts = idx.pts_ticks(ef - 1) - idx.pts_ticks(sf)
                    last_dur = idx.frame_duration_ticks(ef - 1)
                    stages.append(build_vfr_fix_cmd(self._ffmpeg, raw_seg, final_seg,
                                                    last_pts, last_dur))
                    kinds.append("vfr_fix")
                    outputs.append(final_seg)
                else:
                    stages.append(build_segment_cmd(
                        self._ffmpeg, media, scope, pxs, proj.settings, proj.video_path,
                        final_seg, crop=crop, include_audio=not separate_audio))
                    kinds.append("segment")
                    outputs.append(final_seg)
                durs.append(max(0.0, scope.end - scope.start))

            list_file = os.path.join(self._tmpdir, "list.txt")
            with open(list_file, "w", encoding="utf-8") as f:
                for i in range(len(render_scopes)):
                    f.write(f"file 'seg_{i:03d}.mp4'\n")
            concat_out = os.path.join(self._tmpdir, "concat_out.mp4")
            stages.append(build_concat_cmd(self._ffmpeg, list_file, concat_out))
            kinds.append("concat")
            outputs.append(concat_out)

            if separate_audio:
                final_tmp = os.path.join(self._tmpdir, "final_tmp.mp4")
                if full_range:
                    # 起止点未变:整条原音轨流复制,零损失
                    stages.append(build_mux_original_audio_cmd(
                        self._ffmpeg, concat_out, proj.video_path, final_tmp))
                    kinds.append("mux_original")
                else:
                    # 起止点改变:整个范围音频单独重编码一次(参照源质量)
                    audio_out = os.path.join(self._tmpdir, "audio.m4a")
                    audio_start, audio_dur = lo, hi - lo
                    start_sample = end_sample = None
                    if proj.process_range_pts_ticks is not None and proj.video_time_base is not None:
                        num, den = proj.video_time_base
                        audio_start = proj.process_range_pts_ticks[0] * num / den
                        audio_end = proj.process_range_pts_ticks[1] * num / den
                        audio_dur = audio_end - audio_start
                        sr = media.sample_rate or 48000
                        start_sample = int(round(audio_start * sr))
                        end_sample = int(round(audio_end * sr))
                    stages.append(build_audio_only_cmd(
                        self._ffmpeg, media, proj.settings, proj.video_path,
                        audio_out, audio_start, audio_dur,
                        start_sample=start_sample, end_sample=end_sample))
                    kinds.append("audio")
                    outputs.append(audio_out)
                    stages.append(build_mux_audio_file_cmd(
                        self._ffmpeg, concat_out, audio_out, final_tmp))
                    kinds.append("mux_audio")
                outputs.append(final_tmp)
                self._final_source = final_tmp
            else:
                self._final_source = concat_out

            self._stages = stages
            self._stage_kinds = kinds
            self._stage_outputs = outputs
            self._seg_durs = durs
            self._stage_idx = 0
            self._done_dur = 0.0
            self._total_dur = total
            self._cancelled = False
            self._nvenc = proj.settings.encoder_mode == "hw"
            self._media = media
            self._stderr_tail = []
            self._run_next_stage()
        except Exception as e:
            self._fail_current(str(e))

    def _run_next_stage(self) -> None:
        if self._stage_idx >= len(self._stages):
            self._finalize()
            return
        cmd = self._stages[self._stage_idx]
        if self._stage_kinds and self._stage_idx < len(self._stage_kinds):
            self._stage_kind = self._stage_kinds[self._stage_idx]
        else:
            self._stage_kind = "segment" if self._stage_idx < len(self._seg_durs) else "concat"
        self.logLine.emit("┌ " + " ".join(cmd[:8]) + " …")
        self._proc.start(cmd[0], cmd[1:])

    def _finalize(self) -> None:
        try:
            os.replace(self._final_source or os.path.join(self._tmpdir, "concat_out.mp4"),
                       self._final_out)
            if not os.path.exists(self._final_out):
                raise RuntimeError("输出文件未生成")
            expected_size = None
            proj = self._cur.project
            # 边缘线全程统一:expected_size 直接取 project.grid 首/尾边(所有段一致)
            if proj.grid.crop_left > 0 or proj.grid.crop_right < 1.0:
                left_px = int(round(proj.grid.crop_left * self._media.width))
                right_px = int(round(proj.grid.crop_right * self._media.width))
                expected_size = (right_px - left_px, self._media.height)
            expected_duration = max(0.0, proj.process_range[1] - proj.process_range[0])
            warns = verify_output(self._media, self._final_out,
                                  proj.settings, expected_size=expected_size,
                                  expected_duration=expected_duration,
                                  expected_frame_count=self._expected_frames)
            msg = f"完成 → {self._final_out}"
            for w in warns:
                msg += f"\n⚠ {w}"
                self.logLine.emit("⚠ " + w)
            self._cur.progress = 1.0
            self.jobProgress.emit(self._cur.id, 1.0)
            self._cur.status = JobStatus.OK
            self.jobStateChanged.emit(self._cur.id, JobStatus.OK.value)
            self.jobFinished.emit(self._cur.id, True, msg)
            self.logLine.emit(f"✓ {msg}")
            self._cleanup_tmp()
            self._cur = None
            self._state = RenderState.IDLE
            self._advance()
        except Exception as e:
            self._fail_current(f"收尾失败:{e}")

    def _fail_current(self, message: str) -> None:
        if self._cur is None:
            return
        self._cur.status = JobStatus.FAILED
        self._cur.message = message
        self._cleanup_tmp()
        self.jobStateChanged.emit(self._cur.id, JobStatus.FAILED.value)
        self.jobFinished.emit(self._cur.id, False, message)
        self.logLine.emit("✗ " + message)
        self._cur = None
        self._state = RenderState.IDLE
        self._advance()

    def _cleanup_tmp(self) -> None:
        if self._tmpdir and os.path.isdir(self._tmpdir):
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        self._tmpdir = ""
        self._stages = []
        self._stage_kinds = []
        self._stage_outputs = []
        self._final_source = ""

    @staticmethod
    def _dedupe_path(path: str) -> str:
        if not os.path.exists(path):
            return path
        stem, ext = os.path.splitext(path)
        i = 2
        while os.path.exists(f"{stem}({i}){ext}"):
            i += 1
        return f"{stem}({i}){ext}"

    # ---------- 进度 ----------
    def _on_stdout(self) -> None:
        data = bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")
        for line in data.splitlines():
            if line.startswith("out_time_ms="):
                try:
                    ms = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if self._stage_kind == "segment":
                    # 段阶段:编码进度映射到 0~95%(concat/核验留 5%)
                    seg_ordinal = self._stage_kinds[:self._stage_idx].count("segment")
                    if seg_ordinal < len(self._seg_durs):
                        seg_dur = self._seg_durs[seg_ordinal]
                        seg_p = max(0.0, min(1.0, ms / 1000.0 / seg_dur))
                        p = (self._done_dur + seg_p * seg_dur) / self._total_dur * 0.95
                        self._cur.progress = p
                        self.jobProgress.emit(self._cur.id, p)
                elif self._stage_kind == "concat":
                    # concat 阶段:总输出时长 → 95~99%(渐进,不再段完成即 100%)
                    cp = max(0.0, min(1.0, ms / 1000.0 / self._total_dur))
                    p = 0.95 + 0.04 * cp
                    self._cur.progress = p
                    self.jobProgress.emit(self._cur.id, p)

    def _on_stderr(self) -> None:
        data = bytes(self._proc.readAllStandardError()).decode("utf-8", "replace")
        for line in data.splitlines():
            if line.strip():
                self._stderr_tail.append(line)
                if len(self._stderr_tail) > _LOG_TAIL:
                    self._stderr_tail.pop(0)
                self.logLine.emit("  " + line)

    def _on_proc_error(self, err) -> None:
        """ffmpeg 启动失败(找不到可执行文件等):报错而不是卡在"处理中"。"""
        if self._cancelled or self._cur is None:
            return
        self._fail_current(f"无法启动 ffmpeg:{err}")

    def _on_finished(self, exit_code: int, exit_status) -> None:
        if self._cur is None:
            return   # errorOccurred 已处理过该失败
        if self._cancelled:
            self._cancelled = False
            self._cur.status = JobStatus.CANCELLED
            self._cur.message = "已取消"
            self._cleanup_tmp()
            self.jobStateChanged.emit(self._cur.id, JobStatus.CANCELLED.value)
            self.jobFinished.emit(self._cur.id, False, "已取消")
            self._cur = None
            self._state = RenderState.IDLE
            self._advance()
            return
        if exit_code != 0:
            # NVENC 失败:暂停询问用户,不自动切换(软件重试后 _nvenc 为 False
            # 不会再来这里——天然只问一次,无需标志位)
            if (self._stage_kind == "segment" and self._nvenc
                    and self._cur and self._cur.project.settings.encoder_mode == "hw"):
                self._cur.status = JobStatus.RETRY_PENDING
                self.jobStateChanged.emit(self._cur.id, JobStatus.RETRY_PENDING.value)
                tail = "\n".join(self._stderr_tail[-10:])
                self.logLine.emit(f"⚠ NVENC 编码失败,等待用户决定。\n{tail}")
                self._state = RenderState.NVENC_PENDING
                self.retryPrompt.emit(self._cur.id)
                return
            tail = "\n".join(self._stderr_tail[-_LOG_TAIL:])
            self._fail_current(f"ffmpeg 失败(退出码 {exit_code})\n{tail}")
            return
        # 成功 → 下一阶段
        if self._stage_kind == "segment":
            seg_ordinal = self._stage_kinds[:self._stage_idx].count("segment")
            if seg_ordinal < len(self._seg_durs):
                self._done_dur += self._seg_durs[seg_ordinal]
        self._stage_idx += 1
        self._run_next_stage()
