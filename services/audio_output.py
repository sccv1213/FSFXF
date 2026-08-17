"""音频输出:QAudioSink 封装(从旧 main_window 剥离的独立类)。

全部时序陷阱固化在此(踩坑记录,见 CLAUDE.md 决策 11):
- PySide6 6.11 QAudioSink 无 write() → sink.start() 返回 QIODevice,write 走它
- 内部缓冲仅 0.25s → 待写缓冲 bytearray + 20ms flush 定时器掏空
- sink.reset() 会置 Stopped 使 write 全部失效 → 必须立即 start() 重启
  (暂停态不重启,播放时由 playing(True) 补)
- seek 后 25ms 防抖合并 reset(连点步进每 seek 一次)
- 失败原则:不静默兜底——无设备/打开失败 emit no_device/error,UI 明示
  "预览将无声音",视频播放不受影响
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

try:
    from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
except ImportError:  # pragma: no cover
    QAudioSink = None

FLUSH_MS = 20               # 待写缓冲掏空周期
RESET_DEBOUNCE_MS = 25      # seek 后 reset 防抖
MAX_PENDING = 48000 * 2 * 2 * 2   # ≈2 秒 s16 立体声(worker 已按 pts 节流,正常不触发)


class AudioOutput(QObject):
    """QAudioSink 输出:open → playing(True) 后 write 有效。"""

    state_changed = Signal(object)   # ("ok"|"no_device"|"error", 说明)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sink = None
        self._io = None
        self._pending = bytearray()
        self._playing = False
        self._volume = 100
        self._reset_timer = QTimer(self)
        self._reset_timer.setSingleShot(True)
        self._reset_timer.timeout.connect(self._do_reset)
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(FLUSH_MS)
        self._flush_timer.timeout.connect(self.flush)

    # ---- 生命周期 ----
    def open(self, sample_rate: int, channels: int) -> None:
        """按媒体音频参数打开输出。失败明示(不静默),视频播放不受影响。"""
        self._close()
        if QAudioSink is None:
            self.state_changed.emit(("error", "QtMultimedia 不可用,预览将无声音"))
            return
        try:
            device = QMediaDevices.defaultAudioOutput()
            if device is None:
                self.state_changed.emit(("no_device", "无默认音频输出设备,预览将无声音"))
                return
            fmt = QAudioFormat()
            fmt.setSampleRate(int(sample_rate or 48000))
            fmt.setChannelCount(int(channels or 2))
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            sink = QAudioSink(device, fmt)
            sink.setVolume(self._volume / 100 if self._volume else 1.0)
            io = sink.start()   # 6.11 唯一写入途径(无 sink.write())
            if io is None:
                sink.deleteLater()
                self.state_changed.emit(("error", "音频输出启动失败,预览将无声音"))
                return
            self._sink = sink
            self._io = io
            self._flush_timer.start()
            self.state_changed.emit(("ok", ""))
        except Exception as e:
            self._close()
            self.state_changed.emit(("error", f"音频输出异常:{e},预览将无声音"))

    def close(self) -> None:
        """关闭输出(窗口关闭时调用)。"""
        self._close()

    def _close(self) -> None:
        self._flush_timer.stop()
        self._reset_timer.stop()
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception:
                pass
            self._sink.deleteLater()
        self._sink = None
        self._io = None
        self._pending.clear()

    # ---- 播放状态同步 ----
    def playing(self, on: bool) -> None:
        """外部播放态同步:暂停清缓冲;sink 停滞后恢复时重启。"""
        self._playing = on
        if self._sink is None or self._io is None:
            return
        try:
            if on:
                if self._sink.state().value == 2:   # Stopped:reset 后必须重启
                    self._io = self._sink.start()
                else:
                    self._sink.resume()
            else:
                self._pending.clear()               # 暂停清空(先于 sink 操作,防异常吞掉)
                self._sink.suspend()
        except Exception:
            pass

    # ---- 数据 ----
    def write(self, data: bytes) -> None:
        """worker 音频帧 → 待写缓冲(超上限丢最旧)。"""
        self._pending.extend(data)
        if len(self._pending) > MAX_PENDING:
            del self._pending[:len(self._pending) - MAX_PENDING]
        if not self._flush_timer.isActive():
            self._flush_timer.start()   # 缓冲从空变非空 → 启动定时器

    def flush(self) -> None:
        """20ms 定时掏空待写缓冲(内部缓冲仅 0.25s);掏空后挂起定时器(低占用)。"""
        if self._io is None:
            return
        if not self._pending:
            if self._flush_timer.isActive():
                self._flush_timer.stop()
            return
        try:
            while self._pending:
                n = self._io.write(bytes(self._pending))
                if n <= 0:
                    return    # 设备忙,等下次
                del self._pending[:n]
            self._flush_timer.stop()   # 掏空 → 挂起
        except Exception:
            self._pending.clear()
            self._flush_timer.stop()

    def reset(self) -> None:
        """seek 后清缓冲 + 重启 sink(25ms 防抖合并连点)。"""
        self._reset_timer.start(RESET_DEBOUNCE_MS)

    def _do_reset(self) -> None:
        if self._sink is None:
            return
        try:
            self._pending.clear()
            self._sink.reset()      # 置 Stopped
            if self._playing:
                # reset 后不 start 则 write 全部失效(无声根因);暂停态不启动,
                # 播放时由 playing(True) 重启
                self._io = self._sink.start()
        except Exception:
            pass

    # ---- 音量 ----
    def set_volume(self, v: int) -> None:
        self._volume = max(0, min(100, int(v)))
        if self._sink is not None:
            try:
                self._sink.setVolume(self._volume / 100)
            except Exception:
                pass
