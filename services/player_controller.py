"""播放状态机:当前时间、播放/暂停、逐帧,与 DecodeWorker 协作(迁移自旧版,原样保留)。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class PlayerController(QObject):
    """负责播放/暂停/逐帧状态与信号转发(UI 只与它打交道)。"""

    frameReady = Signal(object, float)     # (QImage, pts秒)
    timeChanged = Signal(float)            # 当前帧时间(seek 完成/播放中)
    playingChanged = Signal(bool)
    opened = Signal(dict)
    errorOccurred = Signal(str)

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self._worker = worker
        self._playing = False
        self._duration = 0.0
        self._fps = 30.0
        self._last_frame = None
        self._current_time = 0.0
        worker.frameReady.connect(self._on_frame)
        worker.seekDone.connect(self._on_seek_done)
        worker.opened.connect(self._on_opened)
        worker.errorOccurred.connect(self.errorOccurred)

    # ---- 对外接口 ----
    @property
    def duration(self) -> float:
        return self._duration

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def current_time(self) -> float:
        return self._current_time

    @property
    def playing(self) -> bool:
        return self._playing

    def open(self, path: str) -> None:
        self._worker.open(path)

    def play(self) -> None:
        if self._duration <= 0:
            return
        self._playing = True
        self._worker.play()
        self.playingChanged.emit(True)

    def pause(self) -> None:
        self._playing = False
        self._worker.pause()
        self.playingChanged.emit(False)

    def toggle(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.play()

    def seek(self, t: float) -> None:
        self._worker.seek(t)

    def set_rate(self, r: float) -> None:
        """播放倍速透传(worker 节流与音频样本变速)。"""
        self._worker.set_rate(r)

    def step(self, delta: int) -> None:
        """逐帧步进(±N 帧)。步进自动暂停(与 worker 行为同步,UI 状态一致)。"""
        was_playing = self._playing
        self._playing = False
        self._worker.step(delta)
        if was_playing:
            self.playingChanged.emit(False)

    # ---- 工作线程信号 ----
    def _on_frame(self, img, pts: float) -> None:
        self._last_frame = img
        self._current_time = pts
        self.frameReady.emit(img, pts)
        self.timeChanged.emit(pts)

    def _on_seek_done(self, t: float) -> None:
        self._current_time = t
        self.timeChanged.emit(t)

    def _on_opened(self, info: dict) -> None:
        self._duration = info.get("duration", 0.0)
        self._fps = info.get("fps", 30.0)
        self._last_frame = None
        self.opened.emit(info)
