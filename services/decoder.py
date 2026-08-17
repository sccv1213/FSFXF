"""PyAV 解码线程:精确 seek / 播放 / 逐帧步进 / 自动抽帧。

线程模型:DecodeWorker(QThread) 独占一个 av.Container(AVContext 非线程安全),
外部通过 queue 投递命令,工作线程串行处理,结果经 Qt 信号回传。

重写拆分(旧 decoder.py 411 行三对象):
- PlaybackClock:统一播放时钟,显式状态机——旧版 `_clock_paused_at`
  "重建时钟必须同时清暂停计时"的不变式(依赖调用顺序)结构性消失
- StepPlanner:步进意图序列化——旧版 `_pending_step`/`_nav_target_pts`/
  `_pts_hist` 三路分支补丁(定位途中合并/同批连点/历史后退)显式化
- DecodeWorker:命令队列 + 解码主循环(原样保留:精确 seek 流程、
  双流 demux 重建、PyAV18 音频 fltp→s16、顶层 try/except 兜底)
"""
from __future__ import annotations

import os
import queue
import time
from enum import Enum

import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from core.deps import PIP_INSTALL_HINT

try:
    import av
except ImportError:  # pragma: no cover
    av = None

_FALLBACK_FPS = 30.0


def _rate_samples(arr, rate: float):
    """按倍率抽稀/重复交错样本数组(速度同步,音调变化属预期)。

    仅整数倍率干净(本项目 0.5x/1x/2x/3x):rate>1 抽稀(2x/3x)、
    rate<1 重复(0.5x 每样本重复 2 次)、1x 原样。
    """
    if rate <= 0:
        return arr
    if rate > 1:
        return arr[::int(round(rate))]
    if rate < 1:
        return np.repeat(arr, int(round(1 / rate)), axis=0)
    return arr


class PlaybackClock:
    """统一播放时钟:显式状态机(idle/running/paused)。

    语义:anchor_perf = perf_counter() - 当前播放位置(秒);视频/音频帧都按
    "该帧 pts 对应的墙钟"输出 → 音画同步、节奏稳定。

    reset_to(pts) 是唯一重建入口(open/seek/step 全走它)——重建即吸收
    一切历史暂停时长,旧版"时钟重建必须同时清 _clock_paused_at"的
    跨字段不变式结构性消失:暂停状态不可能残留到重建之后。
    """

    class State(Enum):
        IDLE = 0
        RUNNING = 1
        PAUSED = 2

    def __init__(self):
        self._state = PlaybackClock.State.IDLE
        self._anchor = 0.0       # running:perf_counter 锚点
        self._paused_pts = 0.0   # paused:冻结的播放位置(秒)

    @property
    def state(self) -> "PlaybackClock.State":
        return self._state

    def reset_to(self, pts: float) -> None:
        """唯一重建入口:回到 running,锚点 = now - pts。"""
        self._state = PlaybackClock.State.RUNNING
        self._anchor = time.perf_counter() - pts

    def now(self) -> float:
        """当前播放位置(秒);idle → 0,paused → 冻结值,running → 锚点换算。"""
        if self._state is PlaybackClock.State.IDLE:
            return 0.0
        if self._state is PlaybackClock.State.PAUSED:
            return self._paused_pts
        return time.perf_counter() - self._anchor

    def pause(self) -> None:
        if self._state is PlaybackClock.State.RUNNING:
            self._paused_pts = self.now()
            self._state = PlaybackClock.State.PAUSED

    def resume(self) -> None:
        if self._state is PlaybackClock.State.PAUSED:
            self.reset_to(self._paused_pts)

    def wait_until(self, pts: float, abort) -> None:
        """等到该帧 pts 对应的墙钟;时钟未建立则以其为基准建立。

        abort() 为真(有排队命令/seek 目标/停止)时提前返回 →
        命令不会因时钟异常长期积压。
        """
        if self._state is not PlaybackClock.State.RUNNING:
            self.reset_to(pts)
            return
        while True:
            delay = self._anchor + pts - time.perf_counter()
            if delay <= 0.003 or abort():
                return
            time.sleep(min(0.05, delay))   # 分片等待,保持命令队列响应


class StepPlanner:
    """逐帧步进规划:历史 pts + 实测间隔 + 连点合并(纯逻辑,可单测)。

    旧版 _pending_step/_nav_target_pts/_pts_hist 三路分支的显式化:
    - hist_pts:最近已发射视频帧的整数 pts(后退用真实帧,最精确,VFR 也准)
    - nav_target:最近一次定位/步进目标(整数 pts,同批连点累计基准)
    - pending:定位途中合并的步进目标(seek 完成后补一次定位)
    """

    def __init__(self, fallback_fps: float = _FALLBACK_FPS):
        self.hist_pts: list[int] = []
        self.nav_target: int = 0
        self.pending: int | None = None
        self._fps = fallback_fps

    def note_frame(self, frame_pts: int) -> None:
        """记录已发射视频帧的整数 pts(最近 3 个)。"""
        self.hist_pts.append(frame_pts)
        if len(self.hist_pts) > 3:
            self.hist_pts.pop(0)

    def fill_from_seek_pass(self, pts_list: list[int]) -> None:
        """seek 解码路径回填:目标帧 A 的前一帧 A-1 已知 → 步进精确。"""
        if pts_list:
            self.hist_pts = pts_list[-3:]

    def clear_pending(self) -> None:
        """显式定位/播放意图优先于残留步进(连点退化为续播)。"""
        self.pending = None

    def measured_interval(self, tb: float) -> int:
        """实测帧间隔(pts 数):用最近两帧的实际差,VFR 也准确。"""
        if len(self.hist_pts) >= 2:
            return max(1, self.hist_pts[-1] - self.hist_pts[-2])
        return max(1, int(round(1.0 / (self._fps * tb))))

    def plan(self, d: int, last_emit_pts: float, tb: float,
             waiting_seek: bool, seek_pending: bool) -> int:
        """计算步进目标 pts(整数)。

        - 定位途中连点(waiting_seek):合并到 pending,不打断当前解码
        - 同批连点(seek_pending):以 nav_target 为基准累计(实测间隔)
        - 后退:直接取目标帧真实 pts;历史不足(如 seek 刚完成连点 -2)走
          估算——否则 hist_pts[-1+d] 越界、线程静默死亡
        """
        if last_emit_pts < 0:
            return 0
        if waiting_seek:
            target = max(0, self.nav_target + d * self.measured_interval(tb))
            self.nav_target = target
            self.pending = target
            return target
        if seek_pending:
            target = max(0, self.nav_target + d * self.measured_interval(tb))
        elif d < 0 and len(self.hist_pts) >= 1 - d:
            target = self.hist_pts[-1 + d]
        else:
            last_int = self.hist_pts[-1] if self.hist_pts \
                else int(round(last_emit_pts / tb))
            target = max(0, last_int + d * self.measured_interval(tb))
        self.nav_target = target
        return target


class DecodeWorker(QThread):
    """预览解码工作线程。"""

    frameReady = Signal(object, float)   # (QImage, pts秒)
    seekDone = Signal(float)             # 定位完成,当前帧时间(秒)
    opened = Signal(dict)                # {width, height, fps, duration, audio?}
    errorOccurred = Signal(str)
    audioData = Signal(bytes, float)     # (s16 交错音频, pts秒)
    audioReset = Signal()                # seek 后 UI 清空音频输出缓冲

    def __init__(self, parent=None):
        super().__init__(parent)
        self._q: queue.Queue = queue.Queue()
        self._stop = False
        # 容器状态(仅工作线程访问)
        self._container = None
        self._stream = None
        self._astream = None
        self._packet_gen = None
        self._eof = False
        self._fps = _FALLBACK_FPS
        self._duration = 0.0
        self._clock = PlaybackClock()
        self._planner = StepPlanner()
        # 播放状态
        self._playing = False
        self._rate = 1.0              # 播放倍速(0.5/1/2/3;新打开视频重置 1x)
        self._skip = 0                # 抽帧:每 (skip+1) 帧取 1
        self._last_emit_pts = -1.0
        self._seek_pts = 0            # 解码到 >= 该 pts 后发射
        self._waiting_seek_frame = False
        self._seek_pass_pts: list[int] = []   # 本次 seek 解码经过的视频帧 pts
        self._rate_sample = []        # (墙钟, pts) 采样(自动抽帧)
        self._threads = max(4, min(os.cpu_count() or 4, 16))

    # ---------- 外部命令(线程安全) ----------
    def open(self, path: str) -> None:
        self._q.put(("open", path))

    def seek(self, t: float) -> None:
        self._q.put(("seek", float(t)))

    def play(self) -> None:
        self._q.put(("play", None))

    def pause(self) -> None:
        self._q.put(("pause", None))

    def set_rate(self, r: float) -> None:
        """播放倍速(视频/音频节流与音频样本同步变速)。"""
        self._q.put(("rate", float(r)))

    def step(self, d: int) -> None:
        self._q.put(("step", int(d)))

    def close(self) -> None:
        self._q.put(("close", None))

    def stop(self) -> None:
        self._stop = True
        self._q.put(("stop", None))
        self.wait(5000)

    # ---------- 主循环 ----------
    def run(self) -> None:
        while not self._stop:
            try:
                self._drain_commands()
                if self._container is None:
                    time.sleep(0.01)
                    continue
                if self._seek_target is not None:
                    self._do_seek(self._seek_target)
                    self._seek_target = None
                    continue
                if self._waiting_seek_frame:
                    self._emit_until_seek_pts()
                    continue
                if self._planner.pending is not None:   # 定位途中合并的步进:补一次定位
                    self._seek_target = self._planner.pending * float(self._stream.time_base)
                    self._planner.pending = None
                    continue
                if self._playing:
                    self._emit_play_frame()
                else:
                    time.sleep(0.005)
            except Exception as e:
                # 顶层兜底:线程内任何未预期异常都上报而不是静默死亡
                self.errorOccurred.emit(f"解码线程异常:{e}")
                break
        self._close_container()

    _seek_target: float | None = None   # 待处理 seek(run 循环消费)

    def _drain_commands(self) -> None:
        try:
            while True:
                cmd, arg = self._q.get_nowait()
                if cmd == "open":
                    self._do_open(arg)
                elif cmd == "seek":
                    self._seek_target = float(arg)
                    self._planner.clear_pending()   # 显式定位意图优先于残留步进
                elif cmd == "play":
                    self._playing = True
                    self._planner.clear_pending()   # 播放意图优先(连点退化为续播)
                    self._clock.resume()
                    if self._eof:
                        self._seek_target = 0.0
                elif cmd == "pause":
                    self._playing = False
                    self._clock.pause()
                elif cmd == "rate":
                    self._rate = float(arg)   # 倍速:节流目标按 rate 缩放
                    # 时钟锚点按新倍率重设:now() 从 当前帧pts/新倍率 继续。
                    # 否则目标 pts/rate 与时钟位置错位——降倍率(3x/5x→0.5x)
                    # 时要等"当前播放位置×(1/新率-1/旧率)"秒,画面长时间卡住。
                    if self._last_emit_pts >= 0:
                        paused = self._clock.state is PlaybackClock.State.PAUSED
                        self._clock.reset_to(self._last_emit_pts / self._rate)
                        if paused:
                            self._clock.pause()   # 保留暂停态(冻结位置 = 新倍率尺度)
                elif cmd == "step":
                    self._do_step(int(arg))
                elif cmd == "close":
                    self._close_container()
        except queue.Empty:
            pass

    # ---------- 打开 ----------
    def _do_open(self, path: str) -> None:
        self._close_container()
        try:
            if av is None:
                raise RuntimeError(f"未安装 PyAV,预览解码不可用。请安装依赖:{PIP_INSTALL_HINT}")
            container = av.open(path)
            stream = container.streams.video[0]
            stream.codec_context.thread_count = self._threads
            self._container = container
            self._stream = stream
            astream = container.streams.audio[0] if container.streams.audio else None
            self._astream = astream
            self._fps = float(stream.average_rate or _FALLBACK_FPS)
            if self._fps <= 0:
                self._fps = _FALLBACK_FPS
            dur = float(stream.duration or 0) * float(stream.time_base)
            if dur <= 0 and container.duration:
                dur = float(container.duration) / 1_000_000
            self._duration = dur
            self._packet_gen = self._container.demux(*[s for s in (stream, astream) if s])
            self._eof = False
            self._last_emit_pts = -1.0
            self._planner = StepPlanner(self._fps)
            self._clock = PlaybackClock()
            self._skip = 0
            self._rate = 1.0            # 新打开视频默认 1 倍速
            self._rate_sample = []
            info = {
                "width": stream.codec_context.width,
                "height": stream.codec_context.height,
                "fps": self._fps,
                "duration": self._duration,
            }
            if astream is not None:
                info["audio"] = {
                    "sample_rate": int(astream.sample_rate or 48000),
                    "channels": int(astream.channels or 2),
                }
            self.opened.emit(info)
            self._seek_target = 0.0
        except Exception as e:  # av.open / 解码器初始化失败
            self._close_container()
            self.errorOccurred.emit(f"打开视频失败:{e}")

    def _close_container(self) -> None:
        if self._container is not None:
            try:
                self._container.close()
            except Exception:
                pass
        self._container = None
        self._stream = None
        self._astream = None
        self._packet_gen = None
        self._eof = False
        self._playing = False
        self._planner.pending = None

    # ---------- seek ----------
    def _do_seek(self, t: float) -> None:
        """精确 seek:落回目标前最近关键帧,再解码到目标帧。"""
        try:
            tb = self._stream.time_base
            t = max(0.0, min(t, self._duration))
            target_pts = int(round(t / float(tb)))
            self._container.seek(target_pts, stream=self._stream, backward=True)
            self._packet_gen = self._container.demux(
                *[s for s in (self._stream, self._astream) if s])
            self._eof = False
            self._seek_pts = target_pts
            self._waiting_seek_frame = True
            self._planner.nav_target = target_pts
            self._seek_pass_pts = []                    # 本次 seek 解码经过的视频帧 pts
            self._clock = PlaybackClock()               # 时钟整体重建(暂停状态不可能残留)
            self._skip = 0
            self._rate_sample = []
            if self._astream is not None:
                self.audioReset.emit()                   # 清空音频输出缓冲(纯视频无需触发)
        except Exception as e:
            self.errorOccurred.emit(f"定位失败:{e}")

    # ---------- 解码 ----------
    def _next_frame(self):
        """取下一帧(含帧级解码错误容错),返回 (kind, frame);kind ∈ video/audio;EOF 返回 (None, None)。"""
        while True:
            if self._stop:
                return (None, None)
            try:
                pkt = next(self._packet_gen)
            except StopIteration:
                self._eof = True
                return (None, None)
            is_audio = self._astream is not None and pkt.stream is self._astream
            try:
                for frame in pkt.decode():
                    if frame.pts is None:
                        continue
                    return ("audio" if is_audio else "video", frame)
            except Exception:
                continue  # 坏包跳过(配合渲染端 -xerror 严格失败)

    def _frame_to_qimage(self, frame) -> QImage:
        arr = np.ascontiguousarray(frame.to_ndarray(format="rgb24"))
        h, w = arr.shape[:2]
        img = QImage(arr.data, w, h, arr.strides[0], QImage.Format.Format_RGB888)
        return img.copy()   # 深拷贝:numpy 缓冲随 frame 释放

    def _emit_frame(self, frame) -> float:
        pts = float(frame.pts) * float(self._stream.time_base)
        self._last_emit_pts = pts
        self._planner.note_frame(int(frame.pts))
        self.frameReady.emit(self._frame_to_qimage(frame), pts)
        return pts

    # ---------- 定位到目标帧后发射 ----------
    def _emit_until_seek_pts(self) -> None:
        for _ in range(8):  # 一次循环最多解 8 帧,避免长时间阻塞命令队列
            kind, frame = self._next_frame()
            if frame is None:
                self._waiting_seek_frame = False
                self._planner.nav_target = int(round(self._last_emit_pts
                                                     / float(self._stream.time_base)))
                self.seekDone.emit(self._last_emit_pts)
                return
            if kind == "audio":
                continue    # seek 定位只关心视频帧
            self._seek_pass_pts.append(int(frame.pts))   # 记录解码路径(步进用)
            if frame.pts >= self._seek_pts:
                self._emit_frame(frame)
                # 用解码路径补全历史:目标帧 A 的前一帧 A-1 已知 → 步进精确(VFR 也准)
                self._planner.fill_from_seek_pass(self._seek_pass_pts)
                self._planner.nav_target = int(frame.pts)     # 定位完成(帧精确,整数 pts)
                self._waiting_seek_frame = False
                self._clock.reset_to(self._last_emit_pts)     # 时钟对齐到定位点
                self.seekDone.emit(self._last_emit_pts)
                return

    # ---------- 播放 ----------
    def _emit_play_frame(self) -> None:
        """解码一帧视频并发射,沿途音频帧按统一时钟输出。

        统一时钟:视频帧与音频帧都等到"其 pts 对应的墙钟"再发射,
        音画同步且节奏稳定;解码慢时自然落后(触发抽帧降级)。
        """
        # 解码到目标视频帧(第 skip+1 个)
        need = self._skip + 1
        last_video = None
        for _ in range(need * 8):   # 上限:视频帧之间夹杂的音频帧数有限
            kind, frame = self._next_frame()
            if frame is None:
                self._playing = False   # EOF 自动暂停
                self.seekDone.emit(self._last_emit_pts)
                return
            if kind == "audio":
                self._emit_audio(frame)
                continue
            last_video = frame
            need -= 1
            if need <= 0:
                break
        if last_video is not None:
            pts = float(last_video.pts) * float(self._stream.time_base)
            # 倍速:等待目标 = pts / rate(墙钟按倍率加速,时钟本身不变)
            self._clock.wait_until(pts / self._rate, self._clock_abort)
            self._emit_frame(last_video)
            # 抽帧率自适应:rate = 播放节奏(秒视频/秒墙钟,理想≈1,倍速时≈self._rate)。
            # 解码太慢 → rate<0.7*self._rate → 加大抽帧;富余 → 恢复抽帧。
            self._rate_sample.append((time.perf_counter(), self._last_emit_pts))
            if len(self._rate_sample) >= 30:
                t0, p0 = self._rate_sample[0]
                t1, p1 = self._rate_sample[-1]
                span = t1 - t0
                if span > 0.5:
                    rate = (p1 - p0) / span
                    if rate < 0.7 * self._rate:
                        self._skip = min(8, self._skip + 1)
                        self._rate_sample = []
                    elif rate > 1.05 * self._rate and self._skip > 0:
                        self._skip -= 1
                        self._rate_sample = []

    def _clock_abort(self) -> bool:
        """时钟等待的提前返回条件:有排队命令/seek 目标/停止。"""
        return (self._seek_target is not None or self._stop
                or not self._q.empty())

    # ---------- 音频 ----------
    def _emit_audio(self, frame) -> None:
        """音频帧 → s16 交错 bytes,按统一时钟输出(防缓冲堆积)。"""
        try:
            # PyAV 18:to_ndarray() 无 format 参数,返回解码格式 fltp(float32, planar)
            arr = np.ascontiguousarray(frame.to_ndarray())   # (channels, samples)
            arr = np.transpose(arr)                          # (samples, channels) 交错
            arr = _rate_samples(arr, self._rate)             # 倍速:抽稀/重复样本
            data = (np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            pts = float(frame.pts) * float(self._astream.time_base)
            self._clock.wait_until(pts / self._rate, self._clock_abort)
            self.audioData.emit(data, pts)
        except Exception:
            pass    # 音频帧格式转换失败时静默跳过,不影响视频预览

    # ---------- 逐帧步进 ----------
    def _do_step(self, d: int) -> None:
        """逐帧步进(±N 帧)。统一走异步 seek 路径(前进/后退一致)。

        - 步进时自动暂停播放(否则"播放中点击"画面看起来不倒退)
        - 目标计算全部委托 StepPlanner.plan(历史/间隔/合并单一逻辑)
        """
        if self._stream is None:
            return
        self._playing = False
        tb = float(self._stream.time_base)
        if self._last_emit_pts < 0:
            self._seek_target = 0.0
            self._planner.nav_target = 0
            self._planner.pending = None
            return
        target_pts = self._planner.plan(
            d, self._last_emit_pts, tb,
            waiting_seek=self._waiting_seek_frame,
            seek_pending=self._seek_target is not None)
        self._seek_target = target_pts * tb   # 整数→秒→整数 round 精确,无 ±1 偏差
