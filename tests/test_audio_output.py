"""AudioOutput 待写缓冲测试(迁移自旧 test_audio_pending_flush):部分写/零写/暂停清空。"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from services.audio_output import AudioOutput


class TestAudioPendingFlush(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make(self) -> AudioOutput:
        a = AudioOutput()
        a._sink = object()          # 仅测试 flush 逻辑,不建真实 sink
        a._io = object()            # sink 已 start(playing 早退条件)
        return a

    def test_partial_write_loops(self):
        """write 部分返回时剩余保留续写(音频无声修复核心)。"""
        a = self._make()

        class FakeIO:
            def __init__(self):
                self.written = b""
                self.calls = 0

            def write(self, data):
                self.calls += 1
                n = min(len(data), 100)   # 每次最多写 100 字节(模拟缓冲满)
                self.written += data[:n]
                return n

        io = FakeIO()
        a._io = io
        a._pending = bytearray(b"A" * 350)
        a.flush()
        self.assertEqual(len(a._pending), 0, "应循环写完")
        self.assertEqual(len(io.written), 350)
        self.assertGreater(io.calls, 1, "应多次写入")

    def test_zero_write_keeps_pending(self):
        """write 返回 0 时应保留剩余(设备忙等下次)。"""
        a = self._make()

        class ZeroIO:
            def write(self, data):
                return 0

        a._io = ZeroIO()
        a._pending = bytearray(b"B" * 50)
        a.flush()
        self.assertEqual(len(a._pending), 50, "write 返回 0 时应保留剩余")

    def test_pause_clears_pending(self):
        """暂停清空待写队列(playing(False))。"""
        a = self._make()
        a._pending = bytearray(b"C" * 10)
        a.playing(False)
        self.assertEqual(len(a._pending), 0)

    def test_write_caps_pending(self):
        """超上限丢最旧(worker 已按 pts 节流,防御)。"""
        from services.audio_output import MAX_PENDING
        a = self._make()
        a._pending = bytearray(b"D" * MAX_PENDING)
        a.write(b"E" * 100)
        self.assertLessEqual(len(a._pending), MAX_PENDING)
        self.assertTrue(a._pending.endswith(b"E" * 100), "应保留最新数据")


if __name__ == "__main__":
    unittest.main()
